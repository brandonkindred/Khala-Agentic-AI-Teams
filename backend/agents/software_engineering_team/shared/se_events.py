"""Pipeline lifecycle event log for the Software Engineering team (DORA substrate).

Records discrete events — task created/merged, gate rejections and re-entries,
agent crash detected/resolved, merges to main — into the ``se_events`` Postgres
table so the DORA-metrics endpoint can derive deployment frequency, lead time,
change-failure rate, and MTTR.

Every write is guarded by ``is_postgres_enabled()`` and wrapped so a logging
failure never breaks the pipeline: instrumentation is best-effort.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Event-type vocabulary.
TASK_CREATED = "task_created"
TASK_MERGED = "task_merged"
MERGE_TO_MAIN = "merge_to_main"
GATE_REJECTED = "gate_rejected"
GATE_REENTRY = "gate_reentry"
CRASH_DETECTED = "crash_detected"
CRASH_RESOLVED = "crash_resolved"
PRODUCTION_ROLLBACK = "production_rollback"  # reserved for when real CI/CD lands

EVENT_TYPES = frozenset(
    {
        TASK_CREATED,
        TASK_MERGED,
        MERGE_TO_MAIN,
        GATE_REJECTED,
        GATE_REENTRY,
        CRASH_DETECTED,
        CRASH_RESOLVED,
        PRODUCTION_ROLLBACK,
    }
)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def record_event(
    event_type: str,
    *,
    job_id: str = "",
    task_id: str = "",
    phase: str = "",
    gate: str = "",
    detail: Optional[dict] = None,
    ts: Optional[datetime] = None,
) -> bool:
    """Append one lifecycle event to ``se_events``.

    Preconditions:
        - ``event_type`` is a non-empty string (ideally one of :data:`EVENT_TYPES`).
        - ``ts``, when given, is a timezone-aware datetime (the real time of the
          event); when omitted the current time is used.
    Postconditions:
        - Returns ``True`` when a row was written, ``False`` when Postgres is
          disabled or the write failed (failure is logged at DEBUG, never raised).
    """
    if not event_type:
        raise ValueError("event_type must be a non-empty string")
    try:
        from shared_postgres import Json, get_conn, is_postgres_enabled
    except Exception:
        return False
    if not is_postgres_enabled():
        return False
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO se_events (ts, job_id, task_id, event_type, phase, gate, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (ts or _utc_now(), job_id, task_id, event_type, phase, gate, Json(detail or {})),
            )
        return True
    except Exception:
        logger.debug("failed to record se_event %s for job %s", event_type, job_id, exc_info=True)
        return False


def fetch_events_since(cutoff: datetime) -> list[dict[str, Any]]:
    """Return all events with ``ts >= cutoff``, oldest first.

    Preconditions:
        - ``cutoff`` is a timezone-aware datetime.
    Postconditions:
        - Returns a list of dict rows (keys: ts, job_id, task_id, event_type,
          phase, gate, detail); ``[]`` when Postgres is disabled or on error.
    """
    try:
        from shared_postgres import dict_row, get_conn, is_postgres_enabled
    except Exception:
        return []
    if not is_postgres_enabled():
        return []
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT ts, job_id, task_id, event_type, phase, gate, detail "
                "FROM se_events WHERE ts >= %s ORDER BY ts",
                (cutoff,),
            )
            return list(cur.fetchall())
    except Exception:
        logger.debug("failed to fetch se_events since %s", cutoff, exc_info=True)
        return []


def prune_events(retention_days: float) -> int:
    """Delete events older than ``retention_days``; returns rows removed (0 if disabled)."""
    if retention_days <= 0:
        return 0
    try:
        from datetime import timedelta

        from shared_postgres import get_conn, is_postgres_enabled
    except Exception:
        return 0
    if not is_postgres_enabled():
        return 0
    cutoff = _utc_now() - timedelta(days=retention_days)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM se_events WHERE ts < %s", (cutoff,))
            return cur.rowcount or 0
    except Exception:
        logger.debug("failed to prune se_events", exc_info=True)
        return 0


__all__ = [
    "TASK_CREATED",
    "TASK_MERGED",
    "MERGE_TO_MAIN",
    "GATE_REJECTED",
    "GATE_REENTRY",
    "CRASH_DETECTED",
    "CRASH_RESOLVED",
    "PRODUCTION_ROLLBACK",
    "EVENT_TYPES",
    "record_event",
    "fetch_events_since",
    "prune_events",
]
