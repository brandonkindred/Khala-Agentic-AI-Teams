"""Optional Postgres sink for per-LLM-call traces (``se_agent_traces``).

When ``SE_TRACE_TO_POSTGRES`` is truthy, an :mod:`llm_service` call observer
persists every SE-attributed LLM call as a row in ``se_agent_traces``. This is
the substrate the DORA/cost endpoint reads for per-job and total spend, so cost
metrics work even without an OTLP collector. Default off; always a no-op when
Postgres is disabled. Writes never raise into the LLM call path.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _trace_enabled() -> bool:
    """True when ``SE_TRACE_TO_POSTGRES`` opts the Postgres trace sink in (default off)."""
    return (os.environ.get("SE_TRACE_TO_POSTGRES", "") or "").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def _retention_days() -> float:
    raw = os.environ.get("SE_TRACE_RETENTION_DAYS", "30")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def write_trace(record: Any) -> bool:
    """Persist one LLM call ``record`` to ``se_agent_traces``.

    Preconditions:
        - ``record`` exposes the :class:`llm_service.telemetry.LLMCallRecord`
          attributes (``timestamp``, ``team``, ``model``, token counts, etc.).
    Postconditions:
        - Returns ``True`` when a row was written; ``False`` when the sink is
          disabled, Postgres is disabled, or the write failed (logged at DEBUG).
    """
    if not _trace_enabled():
        return False
    try:
        from shared_postgres import get_conn, is_postgres_enabled
    except Exception:
        return False
    if not is_postgres_enabled():
        return False
    try:
        ts = datetime.fromtimestamp(getattr(record, "timestamp", 0.0) or 0.0, tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO se_agent_traces (ts, team, agent_key, job_id, task_id, phase, model, "
                "input_tokens, output_tokens, total_tokens, cost_usd, latency_ms, status, outcome, "
                "objective, request_id) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    ts,
                    getattr(record, "team", "") or "",
                    getattr(record, "agent_key", "") or "",
                    getattr(record, "job_id", "") or "",
                    getattr(record, "task_id", "") or "",
                    getattr(record, "phase", "") or "",
                    getattr(record, "model", "") or "",
                    int(getattr(record, "prompt_tokens", 0) or 0),
                    int(getattr(record, "completion_tokens", 0) or 0),
                    int(getattr(record, "total_tokens", 0) or 0),
                    float(getattr(record, "cost_usd", 0.0) or 0.0),
                    int(getattr(record, "latency_ms", 0) or 0),
                    getattr(record, "status", "") or "",
                    getattr(record, "outcome", "") or "",
                    getattr(record, "objective", "") or "",
                    getattr(record, "request_id", "") or "",
                ),
            )
        return True
    except Exception:
        logger.debug("failed to write se_agent_trace", exc_info=True)
        return False


def fetch_cost_since(cutoff: datetime) -> dict[str, Any]:
    """Aggregate cost over ``se_agent_traces`` with ``ts >= cutoff``.

    Postconditions:
        - Returns ``{"total_cost_usd": float, "by_job": {job_id: cost}}``;
          zeros / empty when Postgres is disabled or on error.
    """
    empty: dict[str, Any] = {"total_cost_usd": 0.0, "by_job": {}}
    try:
        from shared_postgres import dict_row, get_conn, is_postgres_enabled
    except Exception:
        return empty
    if not is_postgres_enabled():
        return empty
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT job_id, SUM(cost_usd) AS cost FROM se_agent_traces "
                "WHERE ts >= %s GROUP BY job_id",
                (cutoff,),
            )
            by_job = {r["job_id"]: float(r["cost"] or 0.0) for r in cur.fetchall()}
        return {"total_cost_usd": round(sum(by_job.values()), 6), "by_job": by_job}
    except Exception:
        logger.debug("failed to fetch cost since %s", cutoff, exc_info=True)
        return empty


def prune_traces(retention_days: float | None = None) -> int:
    """Delete traces older than the retention window; returns rows removed."""
    days = _retention_days() if retention_days is None else retention_days
    if days <= 0:
        return 0
    try:
        from shared_postgres import get_conn, is_postgres_enabled
    except Exception:
        return 0
    if not is_postgres_enabled():
        return 0
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM se_agent_traces WHERE ts < %s", (cutoff,))
            return cur.rowcount or 0
    except Exception:
        logger.debug("failed to prune se_agent_traces", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# llm_service observer wiring
# ---------------------------------------------------------------------------

_registered = False
_register_lock = threading.Lock()


def _trace_observer(record: Any) -> None:
    team = getattr(record, "team", "") or ""
    if not getattr(record, "job_id", "") or not team.startswith("software_engineering"):
        return
    write_trace(record)


def register_trace_observer() -> None:
    """Register the SE trace observer with :mod:`llm_service` (idempotent).

    The observer itself is a no-op unless ``SE_TRACE_TO_POSTGRES`` is set, so
    registering unconditionally at startup is safe and cheap.
    """
    global _registered
    with _register_lock:
        if _registered:
            return
        try:
            from llm_service import register_call_observer

            register_call_observer(_trace_observer)
            _registered = True
        except Exception:
            logger.warning("could not register SE trace observer", exc_info=True)


__all__ = [
    "write_trace",
    "fetch_cost_since",
    "prune_traces",
    "register_trace_observer",
]
