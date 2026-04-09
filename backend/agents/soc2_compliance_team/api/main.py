"""FastAPI endpoints for running and monitoring the SOC2 compliance audit team."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from job_service_client import JobServiceClient, start_stale_job_monitor
from shared_observability import init_otel, instrument_fastapi_app
from soc2_compliance_team.models import SOC2AuditResult
from soc2_compliance_team.orchestrator import SOC2AuditOrchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

init_otel(service_name="soc2-compliance-team", team_key="soc2_compliance")

app = FastAPI(
    title="SOC2 Compliance Audit Team API",
    description="Run a SOC2 compliance audit on a code repository. POST to start, GET status to poll.",
    version="1.0.0",
)
instrument_fastapi_app(app, team_key="soc2_compliance")

_job_manager = JobServiceClient(team="soc2_compliance_team")
_stale_monitor_stop = start_stale_job_monitor(
    _job_manager,
    interval_seconds=15.0,
    stale_after_seconds=300.0,
    reason="Job heartbeat stale while pending/running",
)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all pending or running SOC2 audit jobs as failed (e.g. on server shutdown)."""
    try:
        _job_manager.mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def _run_audit_job(job_id: str, repo_path: str) -> None:
    try:
        _update_job(job_id, status="running", current_stage="Loading repository")
        orchestrator = SOC2AuditOrchestrator()
        _update_job(job_id, current_stage="Running TSC audits")
        result = orchestrator.run(repo_path)
        _update_job(
            job_id,
            status="completed",
            current_stage="Completed",
            result=result.model_dump(),
        )
    except Exception as e:
        logger.exception("Audit job %s failed", job_id)
        _update_job(
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
    """Start a SOC2 compliance audit on the given repository path."""
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

    try:
        from soc2_compliance_team.temporal.client import is_temporal_enabled
        from soc2_compliance_team.temporal.start_workflow import start_audit_workflow

        if is_temporal_enabled():
            start_audit_workflow(job_id, str(repo_path))
            return RunAuditResponse(
                job_id=job_id,
                status="running",
                message=f"Audit started (Temporal). Poll GET /soc2-audit/status/{job_id} for results.",
            )
    except ImportError:
        pass

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
