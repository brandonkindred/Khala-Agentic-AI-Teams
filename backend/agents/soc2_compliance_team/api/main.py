"""FastAPI endpoints for running and monitoring the SOC2 compliance audit team."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from job_service_client import JobServiceClient, start_stale_job_monitor
from shared_app import create_team_app
from soc2_compliance_team.models import SOC2AuditResult
from soc2_compliance_team.orchestrator import SOC2AuditOrchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _start_temporal_worker_backstop() -> None:
    """Start the SOC2 Temporal worker when serving the app standalone.

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE``/``_FUNC`` before uvicorn accepts requests.
    This backstop covers running the app standalone (``uvicorn ...:app``, local
    dev / unified API): without it, a ``TEMPORAL_ADDRESS``-set process has no
    worker, so ``start_audit_workflow`` would stall waiting for a client.

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — a failure is logged so
          it cannot abort app boot (``start_team_worker`` is idempotent per
          team, so double-starting with the entrypoint is harmless).
    """
    try:
        from soc2_compliance_team.temporal.worker import (
            start_soc2_temporal_worker_thread,
        )

        start_soc2_temporal_worker_thread()
    except Exception:  # noqa: BLE001 - backstop must not abort app boot
        logger.warning("SOC2 Temporal worker backstop failed to start", exc_info=True)


app = create_team_app(
    service_name="soc2-compliance-team",
    team_key="soc2_compliance",
    title="SOC2 Compliance Audit Team API",
    description="Run a SOC2 compliance audit on a code repository. POST to start, GET status to poll.",
    version="1.0.0",
    on_startup=_start_temporal_worker_backstop,
)

# The decomposed Temporal pipeline (temporal/workflows.py) can go up to an
# hour between job-row touches while a criterion fan-out or report-writing
# activity is queued/running (AUDIT_SCHEDULE_TO_CLOSE_TIMEOUT /
# REPORT_SCHEDULE_TO_CLOSE_TIMEOUT) — Temporal's own per-activity timeouts are
# the primary "is this stuck" detector on that path, feeding a genuine
# failure into mark_failed_activity. This monitor is a backstop for what
# Temporal can't self-heal (e.g. thread-mode, or the whole worker process
# dying), so its threshold must stay comfortably above those ceilings —
# otherwise it can mark a legitimate long-running Temporal audit
# "failed (stale)" before it gets a chance to complete, and
# _update_job_terminal's first-writer-wins guard would then treat that false
# failure as authoritative and silently discard the real completion.
_STALE_JOB_THRESHOLD_SECONDS = 90 * 60

_job_manager = JobServiceClient(team="soc2_compliance_team")
_stale_monitor_stop = start_stale_job_monitor(
    _job_manager,
    interval_seconds=15.0,
    stale_after_seconds=_STALE_JOB_THRESHOLD_SECONDS,
    reason="Job heartbeat stale while pending/running",
)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _is_temporal_enabled() -> bool:
    """Whether Temporal mode is active (``TEMPORAL_ADDRESS`` set).

    Postconditions:
        - Returns ``True`` only if ``shared_temporal`` is importable and
          ``TEMPORAL_ADDRESS`` is set; ``False`` otherwise (defaults to the
          thread-mode path).
    """
    try:
        from shared_temporal import is_temporal_enabled

        return is_temporal_enabled()
    except ImportError:
        return False


class RunAuditRequest(BaseModel):
    """Request body for starting an audit."""

    repo_path: str = Field(
        ...,
        max_length=4096,
        description="Local filesystem path to the code repository to audit.",
    )


class RunAuditResponse(BaseModel):
    """Response from POST /soc2-audit/run."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running", description="Initial status.")
    message: str = Field(default="Audit started. Poll GET /soc2-audit/status/{job_id} for results.")


class AuditStatusResponse(BaseModel):
    """Response from GET /soc2-audit/status/{job_id}."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(
        ...,
        description="pending | running | completed | failed",
    )
    repo_path: Optional[str] = Field(None, description="Repository path being audited.")
    current_stage: Optional[str] = Field(
        None, description="Current audit stage (e.g. Security TSC)."
    )
    last_updated_at: Optional[str] = Field(None, description="Last status update (ISO).")
    error: Optional[str] = Field(None, description="Error message if failed.")
    result: Optional[SOC2AuditResult] = Field(
        None, description="Full audit result when status is completed."
    )


def _update_job(job_id: str, **fields: Any) -> None:
    _job_manager.update_job(job_id, **fields)


_TERMINAL_STATUSES = frozenset({"completed", "failed"})


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
    and keeps running. Without this guard, ``load_repo_activity``'s
    ``status="running"`` write would resurrect that terminal failure back to
    non-terminal — and a *later*, correctly-guarded ``_update_job_terminal``
    completion write would then be let through, since the job no longer looks
    terminal, silently reporting success on a job the API already told the
    client had failed.

    Postconditions:
        - Applies ``fields`` via ``_update_job`` unless the job is already
          terminal (see :func:`_job_is_terminal`), in which case this is a
          no-op (logged).
    """
    if _job_is_terminal(job_id):
        logger.warning("Skipping non-terminal write for job %s: already terminal", job_id)
        return
    _update_job(job_id, **fields)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all pending or running SOC2 audit jobs as failed (e.g. on server shutdown)."""
    try:
        _job_manager.mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def _run_audit_job(job_id: str, repo_path: str) -> None:
    """Run the thread-mode audit and persist its terminal job status.

    Preconditions:
        - ``job_id`` is an existing job; ``repo_path`` is a directory path.
    Postconditions:
        - The job's terminal status is taken from ``result.status`` ("completed"
          or "failed" — the only two values ``SOC2AuditOrchestrator.run``
          returns), not assumed to be "completed": the orchestrator itself
          catches load/criteria/report failures and returns a
          ``status="failed"`` result rather than raising, so a hardcoded
          "completed" write here would silently mask that failure from
          ``GET /soc2-audit/status/{job_id}``. An exception raised by the
          orchestrator itself (a bug, not a modeled failure) is still caught
          and marked failed separately.
    """
    try:
        _update_job(job_id, status="running", current_stage="Loading repository")
        orchestrator = SOC2AuditOrchestrator()
        _update_job(job_id, current_stage="Running TSC audits")
        result = orchestrator.run(repo_path)
        _update_job_terminal(
            job_id,
            status=result.status,
            current_stage="Completed" if result.status == "completed" else "Failed",
            error=result.error,
            result=result.model_dump(),
        )
    except Exception as e:
        logger.exception("Audit job %s failed", job_id)
        _update_job_terminal(
            job_id,
            status="failed",
            error=str(e),
            current_stage="Failed",
        )


@app.post(
    "/soc2-audit/run",
    response_model=RunAuditResponse,
    summary="Start SOC2 compliance audit",
    description="Starts a background audit of the repository at repo_path. Returns job_id to poll for status.",
)
def run_audit(request: RunAuditRequest) -> RunAuditResponse:
    """Start a SOC2 compliance audit on the given repository path.

    Preconditions:
        - ``request.repo_path`` refers to an existing local directory.
    Postconditions:
        - Creates a job row (``status="pending"``) and returns its ``job_id``
          for polling via ``GET /soc2-audit/status/{job_id}``. Dispatches the
          audit to Temporal when enabled, else to a background thread.
        - Raises ``HTTPException(400)`` if ``repo_path`` isn't a directory.
        - Raises ``HTTPException(503)`` if Temporal dispatch fails; the job is
          marked ``failed`` on a best-effort basis (via
          :func:`_update_job_terminal`, so this write can't silently clobber
          a terminal status the workflow already wrote, and a workflow that
          actually started despite the dispatch error can't later clobber
          this ``failed`` write back to ``completed``) rather than left
          orphaned in ``pending``.
    """
    repo_path = Path(request.repo_path).expanduser().resolve()
    if not repo_path.is_dir():
        raise HTTPException(
            status_code=400, detail=f"Repository path is not a directory: {request.repo_path}"
        )

    job_id = str(uuid.uuid4())
    _job_manager.create_job(
        job_id,
        status="pending",
        repo_path=str(repo_path),
        current_stage=None,
        error=None,
        result=None,
        events=[],
    )

    if _is_temporal_enabled():
        try:
            from soc2_compliance_team.temporal.start_workflow import start_audit_workflow

            start_audit_workflow(job_id, str(repo_path))
        except Exception as e:
            # Don't leave the job orphaned in `pending` if dispatch fails. The
            # terminal write is best-effort — a job-store error here must not mask
            # the 503 we owe the client.
            logger.exception("Failed to dispatch SOC2 audit workflow for job %s", job_id)
            try:
                _update_job_terminal(job_id, status="failed", current_stage="Failed", error=str(e))
            except Exception:
                logger.exception("Also failed to mark job %s failed after dispatch error", job_id)
            raise HTTPException(status_code=503, detail=f"Failed to start audit workflow: {e}")
        return RunAuditResponse(
            job_id=job_id,
            status="running",
            message=f"Audit started (Temporal). Poll GET /soc2-audit/status/{job_id} for results.",
        )

    thread = threading.Thread(target=_run_audit_job, args=(job_id, str(repo_path)))
    thread.daemon = True
    thread.start()

    return RunAuditResponse(
        job_id=job_id,
        status="running",
        message=f"Audit started. Poll GET /soc2-audit/status/{job_id} for results.",
    )


@app.get(
    "/soc2-audit/status/{job_id}",
    response_model=AuditStatusResponse,
    summary="Get audit job status",
    description="Returns current status and, when completed, the full SOC2 audit result.",
)
def get_audit_status(job_id: str) -> AuditStatusResponse:
    """Get the status and result of an audit job."""
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result = None
    if job.get("result"):
        result = SOC2AuditResult.model_validate(job["result"])

    return AuditStatusResponse(
        job_id=job_id,
        status=job.get("status", "pending"),
        repo_path=job.get("repo_path"),
        current_stage=job.get("current_stage"),
        last_updated_at=job.get("updated_at"),
        error=job.get("error"),
        result=result,
    )


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
