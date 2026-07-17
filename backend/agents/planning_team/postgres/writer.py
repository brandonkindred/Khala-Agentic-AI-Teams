"""Best-effort writer for the Planning team's ``planning_runs`` audit table.

One row per completed planning run — client name, run summary, handoff
summary, and the open/resolved discovery questions — written from the
finalize step of both the thread-mode and Temporal-mode pipelines. Every
write is guarded by ``is_postgres_enabled()`` and wrapped so an operational
failure never breaks the caller: this is instrumentation, not part of the
finalize contract. See ``planning_team.postgres`` for the ``planning_runs``
DDL this module writes to.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from shared_postgres import bounded_probe, get_conn, is_postgres_enabled, probe_cursor

logger = logging.getLogger(__name__)

# shared_postgres.get_conn()/pg_cursor() apply no statement_timeout by design (it
# would cap legitimate long-running team queries), so the INSERT itself is bounded
# via probe_cursor — otherwise a lock wait on the ON CONFLICT target row could
# block the statement indefinitely. A fixed, modest bound rather than
# shared_postgres.statement_timeout_ms(): that accessor's "0 disables it" default
# would otherwise combine with probe_cursor's 1ms floor clamp to time this write
# out on effectively every call once an operator disables the shared default.
_AUDIT_WRITE_TIMEOUT_S = 5.0

# probe_cursor only bounds the statement once a connection exists — a wedged pool
# acquisition or a fully dead TCP socket (no server response at all) is outside its
# reach (see shared_postgres.probe_cursor's own docstring). bounded_probe closes
# that gap by running the whole acquire/write/cleanup operation in a detached
# worker thread with a hard wall-clock budget, abandoning it (capped, so a
# sustained outage can't grow unbounded threads) rather than ever blocking the
# caller past this budget. Comfortably covers pool acquisition + connect +
# _AUDIT_WRITE_TIMEOUT_S + rollback/cleanup.
_AUDIT_OPERATION_BUDGET_S = 10.0

_PROBE_LABEL = "planning_runs_audit_write"


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
        - Returns ``False`` — never raises, and never blocks past
          ``_AUDIT_OPERATION_BUDGET_S`` wall-clock regardless of WHERE the operation
          stalls (pool acquisition, connect, a wedged socket, or the statement
          itself) — when Postgres is disabled (``POSTGRES_HOST`` unset), the write
          fails for any operational reason, or the whole acquire/write/cleanup
          operation is abandoned by ``shared_postgres.bounded_probe`` after
          exceeding its budget; such a failure is logged at DEBUG or WARNING, never
          raised.
    Raises:
        - ``ValueError`` if ``job_id`` is blank — a caller contract violation, distinct
          from the operational failures above which are swallowed.
    """
    if not job_id:
        raise ValueError("job_id must be a non-empty string")
    if not is_postgres_enabled():
        return False

    def _write() -> bool:
        try:
            with get_conn() as conn, probe_cursor(conn, timeout_s=_AUDIT_WRITE_TIMEOUT_S) as cur:
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

    try:
        return asyncio.run(
            bounded_probe(
                _write,
                on_failure=lambda: False,
                budget=_AUDIT_OPERATION_BUDGET_S,
                label=_PROBE_LABEL,
            )
        )
    except Exception:
        logger.debug(
            "failed to record planning_run for job %s (bounded_probe)", job_id, exc_info=True
        )
        return False


__all__ = ["record_planning_run"]
