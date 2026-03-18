"""FastAPI endpoints for the AI Sales Team pod."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared_job_management import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    CentralJobManager,
    start_stale_job_monitor,
)

from sales_team.models import (
    CoachingRequest,
    NurtureRequest,
    OutreachRequest,
    PipelineStage,
    ProposalRequest,
    ProspectingRequest,
    QualificationRequest,
    SalesPipelineRequest,
    SalesPipelineResult,
)
from sales_team.orchestrator import SalesPodOrchestrator

app = FastAPI(
    title="AI Sales Team API",
    version="1.0.0",
    description=(
        "Full-stack B2B sales pod powered by AWS Strands agents. "
        "Handles prospecting, cold outreach, lead qualification, nurturing, "
        "discovery, proposals, and closing — grounded in Gong Labs, Jeb Blount, "
        "HubSpot, Anthony Iannarino, Jill Konrath, Sales Hacker, Salesfolk, and Zig Ziglar."
    ),
)

logger = logging.getLogger(__name__)
_job_manager = CentralJobManager(team="sales_team")
_stale_monitor_stop = start_stale_job_monitor(
    _job_manager,
    interval_seconds=15.0,
    stale_after_seconds=300.0,
    reason="Job heartbeat stale while pending/running",
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SalesPipelineRunResponse(BaseModel):
    job_id: str
    status: str
    message: str


class SalesPipelineStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    product_name: str
    last_updated_at: str
    eta_hint: Optional[str] = None
    error: Optional[str] = None
    result: Optional[SalesPipelineResult] = None


class SalesPipelineJobListItem(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    product_name: str
    created_at: Optional[str] = None
    last_updated_at: Optional[str] = None


class CancelJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _update_job(job_id: str, **fields: Any) -> None:
    _job_manager.update_job(job_id, **fields)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all active sales pipeline jobs as failed (called on server shutdown)."""
    _job_manager.mark_stale_active_jobs_failed(stale_after_seconds=0, reason=reason)


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------


def _run_pipeline_job(job_id: str, request: SalesPipelineRequest) -> None:
    try:
        _update_job(
            job_id,
            status=JOB_STATUS_RUNNING,
            current_stage="initializing",
            progress=2,
            eta_hint="Starting pipeline...",
        )

        orchestrator = SalesPodOrchestrator()

        def on_update(stage: str, pct: int) -> None:
            _update_job(job_id, current_stage=stage, progress=pct, last_updated_at=_now())

        result = orchestrator.run(request, job_id=job_id, update_cb=on_update)

        _update_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            current_stage="completed",
            progress=100,
            eta_hint="done",
            result=result.model_dump(),
            last_updated_at=_now(),
        )
    except Exception as exc:
        logger.error("Sales pipeline job %s failed: %s", job_id, exc, exc_info=True)
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            current_stage="failed",
            error=str(exc),
            eta_hint=None,
            last_updated_at=_now(),
        )


# ---------------------------------------------------------------------------
# Pipeline endpoints (async, job-based)
# ---------------------------------------------------------------------------


@app.post("/sales/pipeline/run", response_model=SalesPipelineRunResponse, tags=["pipeline"])
def run_pipeline(request: SalesPipelineRequest) -> SalesPipelineRunResponse:
    """Start a full sales pipeline run from the specified entry stage.

    Returns a job_id to poll for status and results.
    Entry stages: prospecting → outreach → qualification → nurturing → discovery → proposal → negotiation.
    """
    job_id = str(uuid.uuid4())
    now = _now()
    _job_manager.create_job(
        job_id,
        job_type="sales_pipeline",
        status=JOB_STATUS_PENDING,
        current_stage="queued",
        progress=0,
        product_name=request.product_name,
        entry_stage=request.entry_stage.value,
        result=None,
        error=None,
        eta_hint="queued",
        created_at=now,
        last_updated_at=now,
    )
    thread = threading.Thread(target=_run_pipeline_job, args=(job_id, request), daemon=True)
    thread.start()
    return SalesPipelineRunResponse(
        job_id=job_id,
        status=JOB_STATUS_RUNNING,
        message=(
            f"Sales pipeline started (entry: {request.entry_stage.value}). "
            f"Poll GET /sales/pipeline/status/{job_id} for updates."
        ),
    )


@app.get(
    "/sales/pipeline/status/{job_id}",
    response_model=SalesPipelineStatusResponse,
    tags=["pipeline"],
)
def get_pipeline_status(job_id: str) -> SalesPipelineStatusResponse:
    """Poll the status of a running or completed pipeline job."""
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result_data = job.get("result")
    result_obj = None
    if result_data and isinstance(result_data, dict):
        try:
            result_obj = SalesPipelineResult(**result_data)
        except Exception:
            result_obj = None

    return SalesPipelineStatusResponse(
        job_id=job_id,
        status=job.get("status", JOB_STATUS_PENDING),
        current_stage=job.get("current_stage", ""),
        progress=job.get("progress", 0),
        product_name=job.get("product_name", ""),
        last_updated_at=job.get("last_updated_at", now := _now()),
        eta_hint=job.get("eta_hint"),
        error=job.get("error"),
        result=result_obj,
    )


@app.get(
    "/sales/pipeline/jobs",
    response_model=List[SalesPipelineJobListItem],
    tags=["pipeline"],
)
def list_pipeline_jobs(running_only: bool = False) -> List[SalesPipelineJobListItem]:
    """List all sales pipeline jobs, optionally filtered to active jobs."""
    jobs = _job_manager.list_jobs()
    if running_only:
        jobs = [j for j in jobs if j.get("status") in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING)]
    items = [
        SalesPipelineJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            current_stage=j.get("current_stage", ""),
            progress=j.get("progress", 0),
            product_name=j.get("product_name", ""),
            created_at=j.get("created_at"),
            last_updated_at=j.get("last_updated_at"),
        )
        for j in jobs
    ]
    items.sort(key=lambda x: x.created_at or x.last_updated_at or "", reverse=True)
    return items


@app.post(
    "/sales/pipeline/job/{job_id}/cancel",
    response_model=CancelJobResponse,
    tags=["pipeline"],
)
def cancel_pipeline_job(job_id: str) -> CancelJobResponse:
    """Cancel a pending or running pipeline job."""
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current = job.get("status", JOB_STATUS_PENDING)
    if current not in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING):
        raise HTTPException(
            status_code=400, detail=f"Job is already in terminal state: {current}"
        )
    _job_manager.update_job(job_id, status="cancelled", heartbeat=False)
    return CancelJobResponse(job_id=job_id)


@app.delete(
    "/sales/pipeline/job/{job_id}",
    response_model=DeleteJobResponse,
    tags=["pipeline"],
)
def delete_pipeline_job(job_id: str) -> DeleteJobResponse:
    """Delete a pipeline job from the store."""
    if not _job_manager.delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteJobResponse(job_id=job_id, message="Job deleted.")


# ---------------------------------------------------------------------------
# Synchronous single-stage endpoints
# ---------------------------------------------------------------------------


@app.post("/sales/prospect", tags=["stages"])
def prospect(request: ProspectingRequest) -> Dict[str, Any]:
    """Identify prospects matching the ICP using the Prospector agent.

    Grounded in Jeb Blount's Fanatical Prospecting and Sales Hacker ICP frameworks.
    """
    orchestrator = SalesPodOrchestrator()
    prospects = orchestrator.prospect_only(
        icp=request.icp,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        max_prospects=request.max_prospects,
        company_context=request.company_context,
    )
    return {"prospects": [p.model_dump() for p in prospects], "count": len(prospects)}


@app.post("/sales/outreach", tags=["stages"])
def generate_outreach(request: OutreachRequest) -> Dict[str, Any]:
    """Generate personalized cold outreach sequences for a list of prospects.

    Grounded in Salesfolk, Jill Konrath SNAP, and the Jeb Blount 6-touch cadence.
    """
    orchestrator = SalesPodOrchestrator()
    sequences = orchestrator.outreach_only(
        prospects=request.prospects,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        case_study_snippets=request.case_study_snippets,
        company_context=request.company_context,
    )
    return {"sequences": [s.model_dump() for s in sequences], "count": len(sequences)}


@app.post("/sales/qualify", tags=["stages"])
def qualify_lead(request: QualificationRequest) -> Dict[str, Any]:
    """Qualify a prospect using BANT, MEDDIC, and Iannarino's value-creation tiers.

    Returns a score, recommended action (advance / nurture / disqualify), and coaching notes.
    """
    orchestrator = SalesPodOrchestrator()
    score = orchestrator.qualify_only(
        prospect=request.prospect,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        call_notes=request.call_notes,
    )
    if not score:
        raise HTTPException(status_code=500, detail="Qualification agent failed to return a result")
    return score.model_dump()


@app.post("/sales/nurture", tags=["stages"])
def build_nurture(request: NurtureRequest) -> Dict[str, Any]:
    """Build long-cycle nurture sequences for leads not ready to buy.

    Grounded in HubSpot inbound methodology and Gong Labs cadence research.
    """
    orchestrator = SalesPodOrchestrator()
    sequences = orchestrator.nurture_only(
        prospects=request.prospects,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        duration_days=request.duration_days,
    )
    return {"sequences": [s.model_dump() for s in sequences], "count": len(sequences)}


@app.post("/sales/proposal", tags=["stages"])
def write_proposal(request: ProposalRequest) -> Dict[str, Any]:
    """Generate a structured sales proposal with ROI model.

    Grounded in Anthony Iannarino's Level-4 Value Creation proposal methodology.
    """
    orchestrator = SalesPodOrchestrator()
    proposal = orchestrator.propose_only(request)
    if not proposal:
        raise HTTPException(status_code=500, detail="Proposal agent failed to return a result")
    return proposal.model_dump()


@app.post("/sales/coaching", tags=["stages"])
def get_coaching(request: CoachingRequest) -> Dict[str, Any]:
    """Generate a Gong Labs-style pipeline coaching report.

    Flags deal risk signals, gives talk/listen ratio advice, and recommends next actions.
    """
    orchestrator = SalesPodOrchestrator()
    report = orchestrator.coach_only(
        prospects=request.prospects,
        product_name=request.product_name,
        pipeline_context=request.pipeline_context,
    )
    if not report:
        raise HTTPException(status_code=500, detail="Coach agent failed to return a result")
    return report.model_dump()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"])
def health() -> Dict[str, str]:
    from sales_team.agents import HAS_STRANDS

    return {
        "status": "ok",
        "strands_sdk": "available" if HAS_STRANDS else "stub_mode",
    }
