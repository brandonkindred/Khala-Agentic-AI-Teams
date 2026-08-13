"""Platform-wide Postgres persistence for LLM call token usage.

Owns ``llm_call_records`` (DDL here so unified_api schema registration and the
lazy self-heal cannot drift). Writes are batched by ``usage_flusher``; this
module never runs on the LLM call thread.

Invariants:
    - Every public read/write is a no-op / empty result when Postgres is unset.
    - Query and write failures are logged and return empty / 0; they never raise
      into the HTTP handler or the flusher heartbeat.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from shared.postgres import is_postgres_enabled, pg_cursor

logger = logging.getLogger(__name__)

TABLE_NAME = "llm_call_records"

USAGE_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS llm_call_records ("
    "id BIGSERIAL PRIMARY KEY, "
    "ts TIMESTAMPTZ NOT NULL, "
    "team TEXT NOT NULL DEFAULT '', "
    "agent_key TEXT NOT NULL DEFAULT '', "
    "model TEXT NOT NULL DEFAULT '', "
    "prompt_tokens INTEGER NOT NULL DEFAULT 0, "
    "completion_tokens INTEGER NOT NULL DEFAULT 0, "
    "total_tokens INTEGER NOT NULL DEFAULT 0, "
    "latency_ms INTEGER NOT NULL DEFAULT 0, "
    "status TEXT NOT NULL DEFAULT '', "
    "caller_tag TEXT NOT NULL DEFAULT '', "
    "cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, "
    "outcome TEXT NOT NULL DEFAULT '', "
    "error_type TEXT, "
    "job_id TEXT, "
    "objective TEXT NOT NULL DEFAULT '', "
    "request_id TEXT NOT NULL DEFAULT '', "
    "task_id TEXT NOT NULL DEFAULT '', "
    "phase TEXT NOT NULL DEFAULT '')"
)
USAGE_TABLE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_llm_call_records_ts ON llm_call_records (ts)"
)
# ALTER for tables created before these columns existed. CREATE TABLE IF NOT
# EXISTS does not add columns to an already-created table.
USAGE_TABLE_ALTER_DDL = (
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS latency_ms INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS caller_tag TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS error_type TEXT",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS job_id TEXT",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS objective TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS request_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS task_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE llm_call_records ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT ''",
)
USAGE_TABLE_STATEMENTS = (USAGE_TABLE_DDL, USAGE_TABLE_INDEX_DDL, *USAGE_TABLE_ALTER_DDL)

_OPTIONAL_RECENT_KEYS = ("error_type", "job_id", "objective", "request_id", "task_id", "phase")

WINDOWS: dict[str, float] = {
    "24h": 24.0,
    "7d": 168.0,
    "30d": 720.0,
    "all": 0.0,
}

_INSERT_SQL = (
    "INSERT INTO llm_call_records (ts, team, agent_key, model, "
    "prompt_tokens, completion_tokens, total_tokens, latency_ms, status, "
    "caller_tag, cost_usd, outcome, error_type, job_id, objective, "
    "request_id, task_id, phase) VALUES "
    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_table_ensured = False
_ensure_lock = threading.Lock()


def window_hours(window: str) -> float:
    """Map a window id to hours.

    Preconditions: ``window`` is a non-empty str.
    Postconditions: returns hours for a known preset (``24h`` / ``7d`` /
        ``30d`` / ``all``) or a finite numeric-hours string (``1.0``, ``24``,
        ``0``). The ``all`` preset maps to ``0.0`` as a display value; numeric
        ``0`` / ``0.0`` is a zero-width window (cutoff is now), matching the
        pre-change route. Raises ``ValueError`` whose message contains
        ``unknown window`` otherwise (unknown token, negative, NaN, inf, or
        a numeric window whose cutoff is not a representable datetime).
    """
    if window in WINDOWS:
        return WINDOWS[window]
    try:
        hours = float(window)
    except (TypeError, ValueError):
        raise ValueError(f"unknown window: {window!r}")
    if not math.isfinite(hours) or hours < 0:
        raise ValueError(f"unknown window: {window!r}")
    if hours > 0:
        try:
            _cutoff_datetime(hours)
        except (OverflowError, ValueError, OSError):
            raise ValueError(f"unknown window: {window!r}")
    return hours


def window_is_unbounded(window: str) -> bool:
    """True only for the ``all`` preset, not numeric ``0`` / ``0.0``.

    Preconditions: ``window`` is a non-empty str already accepted by
        :func:`window_hours`.
    Postconditions: returns ``True`` iff ``window == "all"``.
    """
    return window == "all"


# Popped by GET /api/llm-usage before the body is returned. Set when a usage
# query fails even though ``SELECT 1`` might still succeed, so the route can
# report ``storage_status=unreachable`` instead of empty-but-available.
QUERY_FAILED_KEY = "_query_failed"


def empty_summary(*, window: str, team: str | None) -> dict:
    """Zeroed summary dict matching the GET /api/llm-usage response body (minus storage fields)."""
    hours = window_hours(window)
    return {
        "team": team or "all",
        "window": window,
        "window_hours": hours,
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
        "avg_latency_ms": 0.0,
        "error_count": 0,
        "by_agent": {},
        "by_model": {},
    }


def _failed_summary(*, window: str, team: str | None) -> dict:
    """Empty summary marked so the HTTP layer reports storage as unreachable.

    Preconditions: ``window`` is a key of :data:`WINDOWS` or a finite
        numeric-hours string accepted by :func:`window_hours`.
    Postconditions: returns :func:`empty_summary` with :data:`QUERY_FAILED_KEY`
        set to ``True``.
    """
    data = empty_summary(window=window, team=team)
    data[QUERY_FAILED_KEY] = True
    return data


def _cutoff_datetime(hours: float) -> datetime:
    """UTC instant ``hours`` before now.

    Preconditions: ``hours`` is finite and ``>= 0``.
    Postconditions: returns ``now - timedelta(hours=hours)``. ``hours == 0``
        is now (a zero-width window). Raises ``OverflowError``,
        ``ValueError``, or ``OSError`` when that instant is not representable
        as a datetime (e.g. ``hours=1e308``).
    """
    return datetime.now(tz=timezone.utc) - timedelta(hours=hours)


def _cutoff(window: str) -> datetime | None:
    if window_is_unbounded(window):
        return None
    return _cutoff_datetime(window_hours(window))


def _ensure_table() -> None:
    """Idempotently create the usage table + index. Never raises."""
    global _table_ensured
    if _table_ensured or not is_postgres_enabled():
        return
    with _ensure_lock:
        if _table_ensured:
            return
        try:
            with pg_cursor() as cur:
                if cur is None:
                    return
                for stmt in USAGE_TABLE_STATEMENTS:
                    cur.execute(stmt)
            _table_ensured = True
        except Exception:
            logger.warning("llm_call_records table ensure failed", exc_info=True)


def _nonneg_float(value: Any) -> float:
    """Coerce ``value`` to a finite non-negative float (else ``0.0``)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(n) or n < 0:
        return 0.0
    return n


def record_to_row(record: Any) -> tuple:
    """Build the INSERT tuple from an LLMCallRecord-shaped object.

    Preconditions: ``record`` exposes ``timestamp``, ``team``, ``agent_key``,
        ``model``, ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
        ``latency_ms``, ``status``, plus the recent-call metadata fields
        (``caller_tag``, ``cost_usd``, ``outcome``, ``error_type``, ``job_id``,
        ``objective``, ``request_id``, ``task_id``, ``phase``). Missing
        numerics → 0; missing strings → ``""``; missing optional nullable
        fields → ``None``.
    Postconditions: returns an 18-tuple ``(ts, team, agent_key, model,
        prompt_tokens, completion_tokens, total_tokens, latency_ms, status,
        caller_tag, cost_usd, outcome, error_type, job_id, objective,
        request_id, task_id, phase)`` with timezone-aware UTC ``ts``.
    """
    raw_ts = getattr(record, "timestamp", None)
    epoch = raw_ts if isinstance(raw_ts, (int, float)) and raw_ts > 0 else time.time()
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return (
        ts,
        getattr(record, "team", "") or "",
        getattr(record, "agent_key", "") or "",
        getattr(record, "model", "") or "",
        int(getattr(record, "prompt_tokens", 0) or 0),
        int(getattr(record, "completion_tokens", 0) or 0),
        int(getattr(record, "total_tokens", 0) or 0),
        int(getattr(record, "latency_ms", 0) or 0),
        getattr(record, "status", "") or "",
        getattr(record, "caller_tag", "") or "",
        _nonneg_float(getattr(record, "cost_usd", 0)),
        getattr(record, "outcome", "") or "",
        getattr(record, "error_type", None) or None,
        getattr(record, "job_id", None) or None,
        getattr(record, "objective", "") or "",
        getattr(record, "request_id", "") or "",
        getattr(record, "task_id", "") or "",
        getattr(record, "phase", "") or "",
    )


def write_rows(rows: Sequence[tuple]) -> int:
    """Batch-insert pre-built row tuples. Never raises.

    Preconditions: each element is an 18-tuple from :func:`record_to_row`.
    Postconditions: returns the number of rows written; 0 when Postgres is off,
        ``rows`` is empty, or the write failed.
    """
    if not rows:
        return 0
    if not is_postgres_enabled():
        return 0
    _ensure_table()
    try:
        with pg_cursor() as cur:
            if cur is None:
                return 0
            cur.executemany(_INSERT_SQL, list(rows))
        return len(rows)
    except Exception:
        logger.debug("failed to batch-write %d llm_call_records", len(rows), exc_info=True)
        return 0


def _where(window: str, team: str | None) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    cutoff = _cutoff(window)
    if cutoff is not None:
        clauses.append("ts >= %s")
        params.append(cutoff)
    if team:
        clauses.append("team = %s")
        params.append(team)
    sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def fetch_summary(*, window: str, team: str | None = None) -> dict:
    """Aggregate token usage for ``window`` (and optional ``team``). Never raises.

    Preconditions: ``window`` is a key of :data:`WINDOWS` or a finite
        numeric-hours string accepted by :func:`window_hours`.
    Postconditions: returns a summary dict (zeros / empty maps on Postgres-off
        or query failure). Query failure and a ``None`` cursor also set
        :data:`QUERY_FAILED_KEY` so the HTTP layer can report storage as
        unreachable. ``avg_latency_ms`` is the mean of persisted ``latency_ms``
        (0.0 when there are no rows). ``by_model`` values have ``calls``,
        ``prompt_tokens``, ``completion_tokens``, ``total_tokens``, and
        ``tokens`` (alias of ``total_tokens``). Totals, ``by_model``, and
        ``by_agent`` come from one ``GROUPING SETS`` statement so they share
        a single snapshot (Postgres ``READ COMMITTED`` would otherwise let
        the background flusher commit between successive SELECT statements).
    """
    empty = empty_summary(window=window, team=team)
    if not is_postgres_enabled():
        return empty
    _ensure_table()
    where_sql, params = _where(window, team)
    try:
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                return _failed_summary(window=window, team=team)
            cur.execute(
                "SELECT CASE "
                "WHEN GROUPING(model) = 0 AND GROUPING(agent_key) = 1 THEN 'model' "
                "WHEN GROUPING(model) = 1 AND GROUPING(agent_key) = 0 THEN 'agent' "
                "ELSE 'total' END AS bucket, "
                "model, agent_key, "
                "COUNT(*) AS calls, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
                "COALESCE(AVG(latency_ms), 0) AS avg_latency_ms, "
                "COUNT(*) FILTER (WHERE status <> 'success') AS error_count "
                f"FROM llm_call_records{where_sql} "
                "GROUP BY GROUPING SETS ((), (model), (agent_key))",
                params,
            )
            rows = cur.fetchall() or []
        totals = next((r for r in rows if r.get("bucket") == "total"), {})
        by_model = {
            (r["model"] or ""): {
                "calls": int(r["calls"] or 0),
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "tokens": int(r["total_tokens"] or 0),
            }
            for r in rows
            if r.get("bucket") == "model"
        }
        by_agent = {
            (r["agent_key"] or ""): {
                "calls": int(r["calls"] or 0),
                "tokens": int(r["total_tokens"] or 0),
            }
            for r in rows
            if r.get("bucket") == "agent" and r.get("agent_key")
        }
        return {
            "team": team or "all",
            "window": window,
            "window_hours": window_hours(window),
            "total_calls": int(totals.get("calls") or 0),
            "total_prompt_tokens": int(totals.get("prompt_tokens") or 0),
            "total_completion_tokens": int(totals.get("completion_tokens") or 0),
            "total_tokens": int(totals.get("total_tokens") or 0),
            "avg_latency_ms": round(float(totals.get("avg_latency_ms") or 0), 1),
            "error_count": int(totals.get("error_count") or 0),
            "by_agent": by_agent,
            "by_model": by_model,
        }
    except Exception:
        logger.debug("failed to fetch llm usage summary", exc_info=True)
        return _failed_summary(window=window, team=team)


def fetch_recent(*, window: str, team: str | None = None, limit: int = 100) -> list[dict] | None:
    """Newest-first call rows for ``window``. Never raises.

    Preconditions: ``window`` is a key of :data:`WINDOWS` or a finite
        numeric-hours string accepted by :func:`window_hours`; ``limit`` >= 1.
    Postconditions: list of dicts matching pre-change ``get_recent_calls`` /
        ``LLMCallRecord.to_dict`` (always ``timestamp``, ``team``, ``agent_key``,
        ``model``, ``caller_tag``, token counts, ``latency_ms``, ``status``,
        ``cost_usd``, ``outcome``; optional ``error_type`` / ``job_id`` /
        ``objective`` / ``request_id`` / ``task_id`` / ``phase`` when set),
        oldest-to-newest (the newest ``limit`` rows, most recent last). Empty
        list when Postgres is off or the window has no rows. Returns ``None``
        when the query fails or the cursor is ``None`` so the HTTP layer can
        distinguish failure from zero calls.
    """
    if not is_postgres_enabled():
        return []
    _ensure_table()
    where_sql, params = _where(window, team)
    params = list(params) + [limit]
    try:
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                return None
            cur.execute(
                "SELECT ts, team, agent_key, model, prompt_tokens, completion_tokens, "
                "total_tokens, latency_ms, status, caller_tag, cost_usd, outcome, "
                "error_type, job_id, objective, request_id, task_id, phase "
                f"FROM llm_call_records{where_sql} "
                "ORDER BY ts DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall() or []
        out = [_recent_dict_from_pg(r) for r in rows]
        out.reverse()
        return out
    except Exception:
        logger.debug("failed to fetch recent llm calls", exc_info=True)
        return None


def _recent_dict_from_pg(r: dict) -> dict:
    """Map a ``llm_call_records`` row to the pre-change recent-call dict.

    Preconditions: ``r`` is a dict-row from the recent SELECT (``ts`` may be
        datetime, epoch float, or ``None``).
    Postconditions: returns a dict with the always-present ``to_dict`` keys
        and optional metadata keys only when they are non-empty.
    """
    ts = r.get("ts")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        epoch = ts.timestamp()
    else:
        epoch = float(ts or 0)
    out: dict[str, Any] = {
        "timestamp": epoch,
        "team": r.get("team") or "",
        "agent_key": r.get("agent_key") or "",
        "model": r.get("model") or "",
        "caller_tag": r.get("caller_tag") or "",
        "prompt_tokens": int(r.get("prompt_tokens") or 0),
        "completion_tokens": int(r.get("completion_tokens") or 0),
        "total_tokens": int(r.get("total_tokens") or 0),
        "latency_ms": int(r.get("latency_ms") or 0),
        "status": r.get("status") or "",
        "cost_usd": _nonneg_float(r.get("cost_usd")),
        "outcome": r.get("outcome") or "",
    }
    for key in _OPTIONAL_RECENT_KEYS:
        value = r.get(key)
        if value:
            out[key] = value
    return out


__all__ = [
    "TABLE_NAME",
    "QUERY_FAILED_KEY",
    "USAGE_TABLE_STATEMENTS",
    "WINDOWS",
    "empty_summary",
    "fetch_recent",
    "fetch_summary",
    "record_to_row",
    "window_hours",
    "window_is_unbounded",
    "write_rows",
]
