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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from software_engineering_team.shared.pg import postgres_available

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
          disabled or the write failed (an operational failure is logged at DEBUG,
          never raised).
    Raises:
        - ``ValueError`` if ``event_type`` is empty — a caller contract violation
          (programming error), distinct from the operational failures above which
          are swallowed.
    """
    if not event_type:
        raise ValueError("event_type must be a non-empty string")
    if not postgres_available():
        return False
    # Normalize to an aware UTC timestamp: a naive datetime would be read by
    # Postgres in the session TimeZone, silently shifting DORA windows.
    when = ts or _utc_now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        from shared_postgres import Json, get_conn

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO se_events (ts, job_id, task_id, event_type, phase, gate, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (when, job_id, task_id, event_type, phase, gate, Json(detail or {})),
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
    Raises:
        - ``ValueError`` if ``cutoff`` is naive — a naive bound is compared in the
          session TimeZone and would silently shift the window (caller bug).
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be a timezone-aware datetime")
    if not postgres_available():
        return []
    try:
        from shared_postgres import dict_row, get_conn

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


def job_has_events(job_id: str, event_type: str = "") -> bool:
    """Return True if any se_event exists for ``job_id`` (optionally of ``event_type``).

    Used as an idempotency guard so a resumed/re-run job does not re-emit its
    lifecycle events (which would double-count deployments / re-entries).

    Postconditions: ``False`` when Postgres is disabled, ``job_id`` is empty, or
        on error; never raises.
    """
    if not job_id:
        return False
    if not postgres_available():
        return False
    try:
        from shared_postgres import get_conn

        sql = "SELECT 1 FROM se_events WHERE job_id = %s"
        params: list[Any] = [job_id]
        if event_type:
            sql += " AND event_type = %s"
            params.append(event_type)
        sql += " LIMIT 1"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone() is not None
    except Exception:
        logger.debug("failed to check se_events for job %s", job_id, exc_info=True)
        return False


def unresolved_crashed_task_ids(job_id: str) -> set[str]:
    """Return task ids for ``job_id`` with more CRASH_DETECTED than CRASH_RESOLVED.

    Lets a later (resumed/retry) run know which tasks crashed in a prior run but
    were never resolved, so a successful retry can still emit CRASH_RESOLVED and
    MTTR pairs across runs.

    Postconditions: empty set when Postgres is disabled or on error; never raises.
    """
    if not job_id:
        return set()
    if not postgres_available():
        return set()
    try:
        from shared_postgres import dict_row, get_conn

        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT task_id, "
                "  SUM((event_type = %s)::int) AS detected, "
                "  SUM((event_type = %s)::int) AS resolved "
                "FROM se_events WHERE job_id = %s AND event_type IN (%s, %s) "
                "GROUP BY task_id",
                (CRASH_DETECTED, CRASH_RESOLVED, job_id, CRASH_DETECTED, CRASH_RESOLVED),
            )
            return {
                r["task_id"]
                for r in cur.fetchall()
                if r["task_id"] and int(r["detected"] or 0) > int(r["resolved"] or 0)
            }
    except Exception:
        logger.debug("failed to read unresolved crashes for job %s", job_id, exc_info=True)
        return set()


def prune_events(retention_days: float) -> int:
    """Delete events older than ``retention_days``; returns rows removed (0 if disabled)."""
    if retention_days <= 0:
        return 0
    if not postgres_available():
        return 0
    cutoff = _utc_now() - timedelta(days=retention_days)
    try:
        from shared_postgres import get_conn

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
    "job_has_events",
    "unresolved_crashed_task_ids",
    "prune_events",
]
