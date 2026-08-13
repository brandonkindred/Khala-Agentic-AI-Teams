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
    "status TEXT NOT NULL DEFAULT '')"
)
USAGE_TABLE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_llm_call_records_ts ON llm_call_records (ts)"
)
USAGE_TABLE_STATEMENTS = (USAGE_TABLE_DDL, USAGE_TABLE_INDEX_DDL)

WINDOWS: dict[str, float] = {
    "24h": 24.0,
    "7d": 168.0,
    "30d": 720.0,
    "all": 0.0,
}

_INSERT_SQL = (
    "INSERT INTO llm_call_records (ts, team, agent_key, model, "
    "prompt_tokens, completion_tokens, total_tokens, status) VALUES "
    "(%s, %s, %s, %s, %s, %s, %s, %s)"
)

_table_ensured = False
_ensure_lock = threading.Lock()


def window_hours(window: str) -> float:
    """Map a preset window id to hours (0.0 means unbounded / all-time).

    Preconditions: ``window`` is a non-empty str.
    Postconditions: returns the hours for a known preset; raises ``ValueError``
        whose message contains ``unknown window`` otherwise.
    """
    if window not in WINDOWS:
        raise ValueError(f"unknown window: {window!r}")
    return WINDOWS[window]


# Popped by GET /api/llm-usage before the body is returned. Set when a usage
# query fails even though ``SELECT 1`` might still succeed, so the route can
# report ``storage_status=unreachable`` instead of empty-but-available.
QUERY_FAILED_KEY = "_query_failed"


def empty_summary(*, window: str, team: str | None) -> dict:
    """Zeroed summary dict matching the GET /api/llm-usage response body (minus storage fields)."""
    hours = WINDOWS.get(window, 0.0)
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

    Preconditions: ``window`` is a key of :data:`WINDOWS`.
    Postconditions: returns :func:`empty_summary` with :data:`QUERY_FAILED_KEY`
        set to ``True``.
    """
    data = empty_summary(window=window, team=team)
    data[QUERY_FAILED_KEY] = True
    return data


def _cutoff(window: str) -> datetime | None:
    hours = window_hours(window)
    if hours <= 0:
        return None
    return datetime.now(tz=timezone.utc) - timedelta(hours=hours)


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
                cur.execute(USAGE_TABLE_DDL)
                cur.execute(USAGE_TABLE_INDEX_DDL)
            _table_ensured = True
        except Exception:
            logger.warning("llm_call_records table ensure failed", exc_info=True)


def record_to_row(record: Any) -> tuple:
    """Build the 8-element INSERT tuple from an LLMCallRecord-shaped object.

    Preconditions: ``record`` exposes ``timestamp``, ``team``, ``agent_key``,
        ``model``, ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
        ``status`` (missing numerics → 0, missing strings → "").
    Postconditions: returns ``(ts, team, agent_key, model, prompt_tokens,
        completion_tokens, total_tokens, status)`` with timezone-aware UTC ``ts``.
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
        getattr(record, "status", "") or "",
    )


def write_rows(rows: Sequence[tuple]) -> int:
    """Batch-insert pre-built row tuples. Never raises.

    Preconditions: each element is an 8-tuple from :func:`record_to_row`.
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

    Preconditions: ``window`` is a key of :data:`WINDOWS`.
    Postconditions: returns a summary dict (zeros / empty maps on Postgres-off
        or query failure). Query failure and a ``None`` cursor also set
        :data:`QUERY_FAILED_KEY` so the HTTP layer can report storage as
        unreachable. ``avg_latency_ms`` is always ``0.0`` (latency is not
        persisted). ``by_model`` values have ``calls``, ``prompt_tokens``,
        ``completion_tokens``, ``total_tokens``.
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
                "SELECT COUNT(*) AS total_calls, "
                "COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens, "
                "COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
                "COUNT(*) FILTER (WHERE status <> 'success') AS error_count "
                f"FROM llm_call_records{where_sql}",
                params,
            )
            totals = cur.fetchone() or {}
            cur.execute(
                "SELECT model, COUNT(*) AS calls, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens "
                f"FROM llm_call_records{where_sql} GROUP BY model",
                params,
            )
            model_rows = cur.fetchall() or []
            cur.execute(
                "SELECT agent_key, COUNT(*) AS calls, "
                "COALESCE(SUM(total_tokens), 0) AS tokens "
                f"FROM llm_call_records{where_sql} GROUP BY agent_key",
                params,
            )
            agent_rows = cur.fetchall() or []
        by_model = {
            (r["model"] or ""): {
                "calls": int(r["calls"] or 0),
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
            }
            for r in model_rows
        }
        by_agent = {
            (r["agent_key"] or ""): {
                "calls": int(r["calls"] or 0),
                "tokens": int(r["tokens"] or 0),
            }
            for r in agent_rows
            if r.get("agent_key")
        }
        return {
            "team": team or "all",
            "window": window,
            "window_hours": window_hours(window),
            "total_calls": int(totals.get("total_calls") or 0),
            "total_prompt_tokens": int(totals.get("total_prompt_tokens") or 0),
            "total_completion_tokens": int(totals.get("total_completion_tokens") or 0),
            "total_tokens": int(totals.get("total_tokens") or 0),
            "avg_latency_ms": 0.0,
            "error_count": int(totals.get("error_count") or 0),
            "by_agent": by_agent,
            "by_model": by_model,
        }
    except Exception:
        logger.debug("failed to fetch llm usage summary", exc_info=True)
        return _failed_summary(window=window, team=team)


def fetch_recent(*, window: str, team: str | None = None, limit: int = 100) -> list[dict]:
    """Newest-first call rows for ``window``. Never raises.

    Preconditions: ``window`` is a key of :data:`WINDOWS`; ``limit`` >= 1.
    Postconditions: list of dicts with ``timestamp`` (unix float), ``team``,
        ``agent_key``, ``model``, ``prompt_tokens``, ``completion_tokens``,
        ``total_tokens``, ``status``. Empty list when Postgres is off or on error.
    """
    if not is_postgres_enabled():
        return []
    _ensure_table()
    where_sql, params = _where(window, team)
    params = list(params) + [limit]
    try:
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                return []
            cur.execute(
                "SELECT ts, team, agent_key, model, prompt_tokens, completion_tokens, "
                f"total_tokens, status FROM llm_call_records{where_sql} "
                "ORDER BY ts DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall() or []
        out: list[dict] = []
        for r in rows:
            ts = r["ts"]
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                epoch = ts.timestamp()
            else:
                epoch = float(ts or 0)
            out.append(
                {
                    "timestamp": epoch,
                    "team": r.get("team") or "",
                    "agent_key": r.get("agent_key") or "",
                    "model": r.get("model") or "",
                    "prompt_tokens": int(r.get("prompt_tokens") or 0),
                    "completion_tokens": int(r.get("completion_tokens") or 0),
                    "total_tokens": int(r.get("total_tokens") or 0),
                    "status": r.get("status") or "",
                }
            )
        return out
    except Exception:
        logger.debug("failed to fetch recent llm calls", exc_info=True)
        return []


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
    "write_rows",
]
