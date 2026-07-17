"""Best-effort writer for the Planning team's ``planning_runs`` audit table.

One row per completed planning run — client name, run summary, handoff
summary, and the open/resolved discovery questions — written from the
finalize step of both the thread-mode and Temporal-mode pipelines. Every
write is guarded by ``pg_cursor`` (which itself gates on
``is_postgres_enabled()``) and wrapped so an operational failure never
breaks the caller: this is instrumentation, not part of the finalize
contract. See ``planning_team.postgres`` for the ``planning_runs`` DDL this
module writes to.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from shared_postgres import pg_cursor

logger = logging.getLogger(__name__)


def record_planning_run(
    job_id: str,
    *,
    client_name: Optional[str],
    summary: str,
    handoff_summary: str,
    open_questions: List[Dict[str, Any]],
    resolved_questions: List[Dict[str, Any]],
) -> bool:
    """Insert one audit row into ``planning_runs`` for a completed job.

    Preconditions:
        - ``job_id`` is a non-empty string (the table's primary key).
        - ``open_questions`` / ``resolved_questions`` are lists of JSON-serializable
          dicts; a falsy value is treated as ``[]``.
        - ``summary`` / ``handoff_summary``: a falsy value (including ``None``) is
          coerced to ``""`` rather than inserted as SQL NULL, which would otherwise
          violate the ``NOT NULL`` constraint and be silently swallowed as a failure.
    Postconditions:
        - Returns ``True`` when the INSERT executed without an operational error —
          including the ``ON CONFLICT (job_id) DO NOTHING`` case where a row for
          ``job_id`` already exists (a Temporal activity retry calling this twice
          for the same job is a silent idempotent no-op on the second call).
        - Returns ``False`` — never raises — when Postgres is disabled
          (``POSTGRES_HOST`` unset) or the write fails for any operational reason;
          such a failure is logged at DEBUG, never raised.
    Raises:
        - ``ValueError`` if ``job_id`` is blank — a caller contract violation, distinct
          from the operational failures above which are swallowed.
    """
    if not job_id:
        raise ValueError("job_id must be a non-empty string")
    try:
        with pg_cursor() as cur:
            if cur is None:
                return False
            from shared_postgres import Json

            cur.execute(
                "INSERT INTO planning_runs "
                "(job_id, client_name, summary, handoff_summary, open_questions, resolved_questions) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (job_id) DO NOTHING",
                (
                    job_id,
                    client_name,
                    summary or "",
                    handoff_summary or "",
                    Json(open_questions or []),
                    Json(resolved_questions or []),
                ),
            )
        return True
    except Exception:
        logger.debug("failed to record planning_run for job %s", job_id, exc_info=True)
        return False


__all__ = ["record_planning_run"]
