"""FastAPI endpoints for running and monitoring the SOC2 compliance audit team."""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Annotated, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, StringConstraints

from job_service_client import start_stale_job_monitor
from shared.app import create_team_app
from soc2_compliance_team import job_store
from soc2_compliance_team.models import SOC2AuditResult
from soc2_compliance_team.orchestrator import SOC2AuditOrchestrator

if not logging.getLogger().handlers:
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

# The decomposed Temporal pipeline (temporal/workflows.py) can go up to 90
# minutes between job-row touches while a criterion fan-out or report-writing
# activity is queued/running (AUDIT_SCHEDULE_TO_CLOSE_TIMEOUT /
# REPORT_SCHEDULE_TO_CLOSE_TIMEOUT — increased from the original 1-hour
# ceiling to 90 minutes because each activity now issues two sequential LLM
# calls instead of one; the per-activity AUDIT_TIMEOUT / REPORT_TIMEOUT
# doubled from 30 to 60 minutes for the same reason) — Temporal's own
# per-activity timeouts are the primary "is this stuck"
# detector on that path, feeding a genuine failure into mark_failed_activity.
#
# In thread mode, _run_audit_job touches the job once before
# SOC2AuditOrchestrator.run() and not again until it returns, so the
# uninterrupted window is the combined criteria + report ceiling
# (_CRITERIA_TIMEOUT_SECONDS + _REPORT_TIMEOUT_SECONDS, each doubled from 30
# to 60 minutes for the same two-call reason — 120 minutes combined) plus
# repository-loading and thread-scheduling overhead before that.
#
# This monitor is a backstop for what neither path can self-heal (thread-mode
# has no per-stage timeout of its own; Temporal mode covers the whole worker
# process dying), so its threshold must stay comfortably above BOTH ceilings
# above — otherwise it can mark a legitimate long-running audit "failed
# (stale)" before it gets a chance to complete, and _update_job_terminal's
# first-writer-wins guard would then treat that false failure as
# authoritative and silently discard the real completion. Keep the same ~30
# minute margin above the thread-mode ceiling that this threshold held
# pre-doubling: 90 min total minus the 60 min pre-doubling combined ceiling
# = a 30 min margin; 120 min new combined ceiling + that same 30 min margin
# = 150 min.
_STALE_JOB_THRESHOLD_SECONDS = 150 * 60

_stale_monitor_stop = start_stale_job_monitor(
    job_store._job_manager,
    interval_seconds=15.0,
    stale_after_seconds=_STALE_JOB_THRESHOLD_SECONDS,
    reason="Job heartbeat stale while pending/running",
)


def _is_temporal_enabled() -> bool:
    """Whether Temporal mode is active (``TEMPORAL_ADDRESS`` set).

    Postconditions:
        - Returns ``True`` only if ``shared.temporal`` is importable and
          ``TEMPORAL_ADDRESS`` is set; ``False`` otherwise (defaults to the
          thread-mode path).
    """
    try:
        from shared.temporal import is_temporal_enabled

        return is_temporal_enabled()
    except ImportError:
        return False


class RunAuditRequest(BaseModel):
    """Request body for starting an audit."""

    repo_path: Annotated[str, StringConstraints(max_length=4096)] = Field(
        ...,
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


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all pending or running SOC2 audit jobs as failed (e.g. on server shutdown)."""
    try:
        job_store._job_manager.mark_all_active_jobs_failed(reason)
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
        job_store._update_job(job_id, status="running", current_stage="Loading repository")
        orchestrator = SOC2AuditOrchestrator()
        job_store._update_job(job_id, current_stage="Running TSC audits")
        result = orchestrator.run(repo_path)
        job_store._update_job_terminal(
            job_id,
            status=result.status,
            current_stage="Completed" if result.status == "completed" else "Failed",
            error=result.error,
            result=result.model_dump(),
        )
    except Exception as e:
        logger.exception("Audit job %s failed", job_id)
        job_store._update_job_terminal(
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
          audit to Temporal when enabled, else to a daemon background thread
          (best-effort, thread mode only: the thread is abandoned without
          waiting for it if the process exits, so a job can be left
          ``running`` in the job store across a restart).
        - Raises ``HTTPException(400)`` if ``repo_path`` isn't a directory.
        - Raises ``HTTPException(503)`` if Temporal dispatch fails. The job is
          marked ``failed`` on a best-effort basis via
          :func:`soc2_compliance_team.job_store._update_job_terminal`. This
          write cannot silently clobber a terminal status already written by
          the workflow, and a workflow that started despite the dispatch
          error cannot later overwrite this failed status back to completed,
          preventing the job from being left orphaned in ``pending``.
    """
    repo_path = Path(request.repo_path).expanduser().resolve()
    if not repo_path.is_dir():
        raise HTTPException(
            status_code=400, detail=f"Repository path is not a directory: {request.repo_path}"
        )

    job_id = str(uuid.uuid4())
    job_store._job_manager.create_job(
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
                job_store._update_job_terminal(
                    job_id, status="failed", current_stage="Failed", error=str(e)
                )
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
    """Get the status and result of an audit job.

    Preconditions:
        - ``job_id`` is a job id previously returned by ``POST /soc2-audit/run``.
    Postconditions:
        - Returns the job's current ``status`` (``pending``/``running``/
          ``completed``/``failed``), ``current_stage``, and ``error`` as last
          persisted in the job store. ``result`` is populated (parsed as
          :class:`SOC2AuditResult`) only once the job has produced one; it is
          ``None`` for a job still pending or running.
        - Raises ``HTTPException(404)`` if no job with ``job_id`` exists.
    """
    job = job_store._job_manager.get_job(job_id)
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
