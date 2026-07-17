"""Best-effort audit writer for the Planning team's ``planning_runs`` table.

Populates one row per completed planning run when Postgres is configured; a
pure no-op (never raises for operational failures) otherwise, so it can be
called unconditionally from a finalize path without affecting the HTTP
response, produced artifacts, or the ``HandoffPackage``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from shared_postgres import pg_cursor, statement_timeout_ms

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
    """Best-effort upsert of one ``planning_runs`` row for ``job_id``.

    Preconditions:
        - ``job_id`` is a non-empty string (a caller bug otherwise).
    Postconditions:
        - Returns ``True`` when a row was written, ``False`` when Postgres is
          disabled, the write failed, or it was cancelled by the transaction-
          local ``statement_timeout`` set below. Never raises for operational
          failures (Postgres down/unreachable/misconfigured, or a stalled/
          lock-contended write) — those are logged at DEBUG and swallowed so
          callers can call this unconditionally from a finalize path without
          risking the run's completion result.
    """
    if not job_id:
        raise ValueError("job_id must be a non-empty string")
    try:
        with pg_cursor() as cur:
            if cur is None:
                return False
            from shared_postgres import Json

            # pg_cursor's shared pool deliberately sets no statement_timeout (it
            # would cap legitimate long-running team queries), so without a local
            # bound a stalled server or row-lock contention could hold this
            # "best-effort" write open indefinitely — worse than a fast failure,
            # since no exception would ever reach the except below. SET LOCAL
            # scopes the bound to this transaction only, so it can never leak onto
            # the next reuse of this pooled connection.
            cur.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms()}")
            cur.execute(
                """
                INSERT INTO planning_runs
                    (job_id, client_name, summary, handoff_summary, open_questions, resolved_questions)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                    client_name = EXCLUDED.client_name,
                    summary = EXCLUDED.summary,
                    handoff_summary = EXCLUDED.handoff_summary,
                    open_questions = EXCLUDED.open_questions,
                    resolved_questions = EXCLUDED.resolved_questions
                """,
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
        logger.debug("failed to record planning_runs audit row for job %s", job_id, exc_info=True)
        return False


__all__ = ["record_planning_run"]
