"""Shared job-row bookkeeping for the SOC2 compliance team.

Both ``api/main.py`` (the FastAPI process) and ``temporal/activities.py``
(the Temporal worker) need the same job-row read/write helpers. Splitting
them out here means an activity no longer has to import ``api.main`` to
reach them — that import would pull in the FastAPI ``app`` object and start
a duplicate stale-job monitor thread as a side effect, inside a worker
process that should only run activities.

This module has no import-time side effects beyond constructing a
``JobServiceClient`` (a local object — its constructor does no I/O, only
validates ``JOB_SERVICE_URL`` is set). The stale-job monitor thread stays in
``api/main.py``: it's an API-server-only concern the worker process must not
duplicate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from job_service_client import JobServiceClient

logger = logging.getLogger(__name__)

_job_manager = JobServiceClient(team="soc2_compliance_team")

_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _update_job(job_id: str, **fields: Any) -> None:
    _job_manager.update_job(job_id, **fields)


def _job_is_terminal(job_id: str) -> bool:
    """Best-effort check of whether ``job_id`` has already reached a terminal status.

    Shared by :func:`_update_job_terminal` and :func:`_update_job_unless_terminal`
    — both need the same "is this job already done" read, just to guard
    opposite kinds of writes (a terminal write vs. a non-terminal one).

    Postconditions:
        - Returns ``True`` iff the job's current status is ``completed`` or
          ``failed``. Never raises: a job-store read error is logged and
          treated as "not terminal" (the same best-effort fallback both
          callers already document — this check is a mitigation, not a lock,
          so it must never block a write it can't evaluate).
    """
    try:
        job = _job_manager.get_job(job_id)
    except Exception:
        logger.warning("Could not read job %s to check terminal status", job_id, exc_info=True)
        return False
    return bool(job and job.get("status") in _TERMINAL_STATUSES)


def _update_job_terminal(job_id: str, status: str, **fields: Any) -> None:
    """Write a terminal job status, unless the job is already terminal.

    Two independent paths can race to write a terminal status for the same
    job: ``write_report_activity``'s ``completed`` write can land just before
    a lost activity-completion ack causes Temporal to treat the activity as
    failed (triggering ``mark_failed_activity``); or ``run_audit``'s
    dispatch-failure ``failed`` write can land just before a workflow that
    actually started server-side (despite a local dispatch timeout) later
    completes. Whichever terminal status is written first must stick — a
    later terminal write must not silently clobber it.

    Preconditions:
        - ``status`` is ``"completed"`` or ``"failed"``.
    Postconditions:
        - Updates the job to ``status`` with ``fields`` unless the job is
          already terminal (see :func:`_job_is_terminal`), in which case this
          is a no-op (logged).
    """
    assert status in _TERMINAL_STATUSES, f"not a terminal status: {status}"
    if _job_is_terminal(job_id):
        logger.warning(
            "Skipping terminal write status=%s for job %s: already terminal", status, job_id
        )
        return
    _update_job(job_id, status=status, **fields)


def _update_job_unless_terminal(job_id: str, **fields: Any) -> None:
    """Write a non-terminal job update, unless the job has already reached a terminal status.

    A Temporal activity can still execute after the API already wrote a
    terminal status for its job — e.g. ``run_audit``'s dispatch-failure path
    marks the job ``failed`` when ``start_workflow_sync`` times out
    client-side, even though the workflow was actually accepted server-side
    and keeps running. Without this guard, a non-terminal write (e.g.
    ``load_repo_activity``'s ``status="running"``, or ``write_report_activity``'s
    ``current_stage="Writing report"``) would resurrect that terminal failure
    back to non-terminal — and a *later*, correctly-guarded
    ``_update_job_terminal`` completion write would then be let through, since
    the job no longer looks terminal, silently reporting success on a job the
    API already told the client had failed.

    Postconditions:
        - Applies ``fields`` via ``_update_job`` unless the job is already
          terminal (see :func:`_job_is_terminal`), in which case this is a
          no-op (logged).
    """
    if _job_is_terminal(job_id):
        logger.warning("Skipping non-terminal write for job %s: already terminal", job_id)
        return
    _update_job(job_id, **fields)
