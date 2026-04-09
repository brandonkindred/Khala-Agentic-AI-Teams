"""FastAPI endpoints for running and monitoring the social media marketing team."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, HTTPException

from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    RESTARTABLE_STATUSES,
    RESUMABLE_STATUSES,
    JobServiceClient,
    start_stale_job_monitor,
    validate_job_for_action,
)
from shared_observability import init_otel, instrument_fastapi_app
from social_media_marketing_team.adapters.branding import (
    BrandContext,
    BrandIncompleteError,
    BrandNotFoundError,
    fetch_brand,
    validate_brand_for_social_marketing,
)
from social_media_marketing_team.models import (
    CampaignPerformanceSnapshot,
    HumanReview,
)
from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator

from .request_models import (
    CancelMarketingJobResponse,
    DeleteMarketingJobResponse,
    MarketingJobListItem,
    MarketingJobStatusResponse,
    PerformanceIngestRequest,
    PerformanceIngestResponse,
    ReviseMarketingTeamRequest,
    RunMarketingTeamRequest,
    RunMarketingTeamResponse,
    TrendLatestResponse,
    TrendRunResponse,
)
from .trend_scheduler import get_latest_digest, run_trend_job, start_scheduler, stop_scheduler

# ---------------------------------------------------------------------------
# App & lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_application: FastAPI) -> AsyncIterator[None]:
    start_scheduler()
    yield
    stop_scheduler()


init_otel(service_name="social-media-marketing-team", team_key="social_marketing")

app = FastAPI(title="Social Media Marketing Team API", version="1.0.0", lifespan=_lifespan)
instrument_fastapi_app(app, team_key="social_marketing")

logger = logging.getLogger(__name__)
try:
    _job_manager = JobServiceClient(team="social_media_marketing_team")
    _stale_monitor_stop = start_stale_job_monitor(
        _job_manager,
        interval_seconds=15.0,
        stale_after_seconds=300.0,
        reason="Job heartbeat stale while pending/running",
    )
except Exception as _init_err:
    logger.warning("Social marketing job manager init failed: %s", _init_err)
    _job_manager = None  # type: ignore[assignment]
    _stale_monitor_stop = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _update_job(job_id: str, **fields) -> None:
    _job_manager.update_job(job_id, **fields)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all pending or running marketing jobs as failed (e.g. on server shutdown)."""
    _job_manager.mark_stale_active_jobs_failed(stale_after_seconds=0, reason=reason)


# ---------------------------------------------------------------------------
# Background job execution
# ---------------------------------------------------------------------------


def _run_team_job(job_id: str, request: RunMarketingTeamRequest, brand_ctx: BrandContext) -> None:
    try:
        _update_job(
            job_id,
            status="running",
            current_stage="building_campaign_proposal",
            progress=30,
            eta_hint="~1 minute",
        )
        orchestrator = SocialMediaMarketingOrchestrator(llm_model_name=request.llm_model_name)
        goals = brand_ctx.to_brand_goals(
            goals=request.goals,
            cadence_posts_per_day=request.cadence_posts_per_day,
            duration_days=request.duration_days,
        )

        _update_job(
            job_id,
            current_stage="running_collaboration_and_planning",
            progress=60,
            eta_hint="~30-60 seconds",
        )
        performance = CampaignPerformanceSnapshot(
            campaign_name=f"{brand_ctx.brand_name} multi-platform growth sprint",
            observations=(_job_manager.get_job(job_id) or {}).get("performance_observations", []),
        )
        result = orchestrator.run(
            goals=goals,
            human_review=HumanReview(
                approved=request.human_approved_for_testing,
                feedback=request.human_feedback,
            ),
            performance=performance,
        )

        _update_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            current_stage="completed",
            progress=100,
            eta_hint="done",
            result=result.model_dump(),
        )
    except Exception as exc:
        logger.exception("Social marketing team job %s failed", job_id)
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            current_stage="failed",
            error=f"{type(exc).__name__}: {exc}",
            eta_hint=None,
        )


def _dispatch_job(job_id: str, request: RunMarketingTeamRequest, brand_ctx: BrandContext) -> str:
    """Start a job via Temporal if enabled, otherwise in a daemon thread.

    Returns a human-readable message describing how the job was dispatched.
    """
    try:
        from social_media_marketing_team.temporal.client import is_temporal_enabled
        from social_media_marketing_team.temporal.start_workflow import start_team_job_workflow

        if is_temporal_enabled():
            start_team_job_workflow(job_id, request.model_dump())
            return f"(Temporal). Poll GET /social-marketing/status/{job_id} for updates."
    except ImportError:
        pass

    thread = threading.Thread(target=_run_team_job, args=(job_id, request, brand_ctx), daemon=True)
    thread.start()
    return f"Poll GET /social-marketing/status/{job_id} for updates."


# ---------------------------------------------------------------------------
# Routes — campaign jobs
# ---------------------------------------------------------------------------


def _build_brand_summary(brand_ctx: BrandContext) -> str:
    """Build a brief human-readable brand summary for the happy-path response."""
    header = f"Using brand '{brand_ctx.brand_name}'"
    if brand_ctx.tagline:
        header += f" -- '{brand_ctx.tagline}'"
    detail_parts = []
    if brand_ctx.voice_and_tone:
        detail_parts.append(f"Voice: {brand_ctx.voice_and_tone[:80]}")
    if brand_ctx.target_audience:
        detail_parts.append(f"Audience: {brand_ctx.target_audience[:100]}")
    if detail_parts:
        return f"{header}. {'. '.join(detail_parts)}."
    return f"{header}."


_PHASE_DISPLAY_NAMES = {
    "strategic_core": "Strategic Core (your positioning, values, and audience)",
    "narrative_messaging": "Narrative & Messaging (your brand story, voice, and key messages)",
}

_REQUIRED_PHASES = ["strategic_core", "narrative_messaging"]


def _build_brand_not_found_error(client_id: str, brand_id: str) -> dict:
    """Build a structured error response for a missing brand."""
    return {
        "error": "brand_not_found",
        "message": f"Brand '{brand_id}' was not found for client '{client_id}'.",
        "user_message": (
            "Before we can create campaigns for you, we need to understand your brand. "
            "Here's how to get started:\n\n"
            f"1. Create a client (if you haven't): POST /api/branding/clients\n"
            f"2. Create a brand: POST /api/branding/clients/{client_id}/brands\n"
            f"3. Run the branding pipeline: POST /api/branding/clients/{client_id}"
            f"/brands/{{brand_id}}/run\n\n"
            "The branding process covers your strategic positioning and messaging -- "
            "it takes about 15-20 minutes and ensures your campaigns authentically "
            "represent who you are."
        ),
        "branding_api_base": "/api/branding",
    }


def _build_brand_incomplete_error(exc: BrandIncompleteError) -> dict:
    """Build a structured error response for an incomplete brand."""
    missing_display = "\n".join(f"- {_PHASE_DISPLAY_NAMES.get(p, p)}" for p in exc.missing_phases)
    return {
        "error": "brand_incomplete",
        "message": f"Brand '{exc.brand_id}' needs more development before campaigns can be created.",
        "user_message": (
            "Your brand is off to a great start, but needs a bit more work before we can build "
            "campaigns. Here's what's remaining:\n\n"
            f"{missing_display}\n\n"
            "This ensures your campaign content sounds authentically like your brand. "
            f"Continue building your brand: POST /api/branding/clients/{exc.client_id}"
            f"/brands/{exc.brand_id}/run\n\n"
            "Once done, come back and we'll build campaigns that bring your brand to life."
        ),
        "required_phases": list(_REQUIRED_PHASES),
        "missing_phases": exc.missing_phases,
        "current_phase": exc.current_phase,
        "branding_api_base": "/api/branding",
    }


def _fetch_and_validate_brand(client_id: str, brand_id: str) -> BrandContext:
    """Fetch a brand and validate it has the required phases.

    Raises ``HTTPException`` with a structured 422 error on failure.
    """
    try:
        brand_data = fetch_brand(client_id, brand_id)
    except BrandNotFoundError as exc:
        raise HTTPException(
            status_code=422, detail=_build_brand_not_found_error(client_id, brand_id)
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        return validate_brand_for_social_marketing(brand_data, client_id, brand_id)
    except BrandIncompleteError as exc:
        raise HTTPException(status_code=422, detail=_build_brand_incomplete_error(exc)) from exc


@app.post("/social-marketing/run", response_model=RunMarketingTeamResponse)
def run_marketing_team(request: RunMarketingTeamRequest) -> RunMarketingTeamResponse:
    brand_ctx = _fetch_and_validate_brand(request.client_id, request.brand_id)

    job_id = str(uuid.uuid4())
    now = _now()
    _job_manager.create_job(
        job_id,
        job_type="run_marketing_team",
        status=JOB_STATUS_PENDING,
        current_stage="queued",
        progress=0,
        llm_model_name=request.llm_model_name,
        client_id=request.client_id,
        brand_id=request.brand_id,
        result=None,
        error=None,
        eta_hint="queued",
        performance_observations=[],
        created_at=now,
        last_updated_at=now,
        revision_history=[],
        request_payload=request.model_dump(),
    )

    dispatch_msg = _dispatch_job(job_id, request, brand_ctx)
    return RunMarketingTeamResponse(
        job_id=job_id,
        status="running",
        message=f"Social marketing team started. {dispatch_msg}",
        brand_summary=_build_brand_summary(brand_ctx),
    )


@app.post("/social-marketing/performance/{job_id}", response_model=PerformanceIngestResponse)
def ingest_performance(job_id: str, payload: PerformanceIngestRequest) -> PerformanceIngestResponse:
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    observations = job.get("performance_observations", [])
    observations.extend([obs.model_dump() for obs in payload.observations])
    _job_manager.update_job(job_id, performance_observations=observations, last_updated_at=_now())

    campaign_name = None
    result = job.get("result")
    if isinstance(result, dict):
        proposal = result.get("proposal")
        if isinstance(proposal, dict):
            campaign_name = proposal.get("campaign_name")

    return PerformanceIngestResponse(
        job_id=job_id,
        campaign_name=campaign_name,
        observations_ingested=len(payload.observations),
        message="Performance observations stored.",
    )


@app.post("/social-marketing/revise/{job_id}", response_model=RunMarketingTeamResponse)
def revise_marketing_team(
    job_id: str, request: ReviseMarketingTeamRequest
) -> RunMarketingTeamResponse:
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    original_payload = job.get("request_payload")
    if not isinstance(original_payload, dict):
        raise HTTPException(
            status_code=400, detail="Original run payload not available for revision"
        )

    original_request = RunMarketingTeamRequest(**original_payload)
    brand_ctx = _fetch_and_validate_brand(original_request.client_id, original_request.brand_id)

    revised = original_request.model_copy(
        update={
            "human_feedback": request.feedback,
            "human_approved_for_testing": request.approved_for_testing,
        }
    )
    revision_history = job.get("revision_history", [])
    revision_history.append(request.feedback)
    _job_manager.update_job(
        job_id,
        status=JOB_STATUS_RUNNING,
        current_stage="revision_queued",
        progress=10,
        eta_hint="~1-2 minutes",
        error=None,
        revision_history=revision_history,
        request_payload=revised.model_dump(),
        last_updated_at=_now(),
    )

    dispatch_msg = _dispatch_job(job_id, revised, brand_ctx)
    return RunMarketingTeamResponse(
        job_id=job_id,
        status="running",
        message=f"Revision started for {job_id}. {dispatch_msg}",
        brand_summary=_build_brand_summary(brand_ctx),
    )


@app.get("/social-marketing/jobs", response_model=List[MarketingJobListItem])
def list_marketing_jobs(
    running_only: bool = False,
) -> List[MarketingJobListItem]:
    """List all marketing jobs, optionally filtered to pending/running only."""
    jobs = _job_manager.list_jobs()
    if running_only:
        jobs = [j for j in jobs if j.get("status") in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING)]
    items = [
        MarketingJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", "pending"),
            current_stage=j.get("current_stage", ""),
            progress=j.get("progress", 0),
            created_at=j.get("created_at") or j.get("last_updated_at"),
            last_updated_at=j.get("last_updated_at"),
        )
        for j in jobs
    ]
    items.sort(key=lambda x: x.created_at or x.last_updated_at or "", reverse=True)
    return items


@app.get("/social-marketing/status/{job_id}", response_model=MarketingJobStatusResponse)
def get_marketing_job_status(job_id: str) -> MarketingJobStatusResponse:
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    data = {k: v for k, v in job.items() if k in MarketingJobStatusResponse.model_fields}

    # Backfill client_id/brand_id from request_payload for jobs created
    # before these fields were stored at the top level.
    if "client_id" not in data or "brand_id" not in data:
        payload = job.get("request_payload")
        if isinstance(payload, dict):
            data.setdefault("client_id", payload.get("client_id"))
            data.setdefault("brand_id", payload.get("brand_id"))
    if not data.get("client_id") or not data.get("brand_id"):
        raise HTTPException(
            status_code=410,
            detail=(
                f"Job {job_id} predates the brand requirement and cannot display "
                f"brand context. Create a new job with client_id and brand_id."
            ),
        )

    return MarketingJobStatusResponse(**data)


@app.post("/social-marketing/job/{job_id}/cancel", response_model=CancelMarketingJobResponse)
def cancel_marketing_job(job_id: str) -> CancelMarketingJobResponse:
    """Cancel a pending or running marketing job."""
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current = job.get("status", JOB_STATUS_PENDING)
    if current not in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING):
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {current}. Cannot cancel.",
        )
    _job_manager.update_job(job_id, status="cancelled", heartbeat=False)
    return CancelMarketingJobResponse(job_id=job_id, message="Job cancellation requested.")


@app.delete("/social-marketing/job/{job_id}", response_model=DeleteMarketingJobResponse)
def delete_marketing_job(job_id: str) -> DeleteMarketingJobResponse:
    """Delete a marketing job from the store."""
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not _job_manager.delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteMarketingJobResponse(job_id=job_id, message="Job deleted.")


@app.post("/social-marketing/job/{job_id}/resume", response_model=RunMarketingTeamResponse)
def resume_marketing_job(job_id: str) -> RunMarketingTeamResponse:
    """Resume an interrupted marketing job by re-dispatching with stored inputs."""
    try:
        job = validate_job_for_action(_job_manager.get_job(job_id), job_id, RESUMABLE_STATUSES, "resumed")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    payload = job.get("request_payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Original request payload not available for resume.")

    request = RunMarketingTeamRequest(**payload)
    _job_manager.update_job(job_id, status=JOB_STATUS_RUNNING, error=None, current_stage="resuming")
    dispatch_msg = _dispatch_job(job_id, request)
    return RunMarketingTeamResponse(job_id=job_id, status="running", message=f"Job resumed. {dispatch_msg}")


@app.post("/social-marketing/job/{job_id}/restart", response_model=RunMarketingTeamResponse)
def restart_marketing_job(job_id: str) -> RunMarketingTeamResponse:
    """Restart a marketing job from scratch with the same inputs."""
    try:
        job = validate_job_for_action(_job_manager.get_job(job_id), job_id, RESTARTABLE_STATUSES, "restarted")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    payload = job.get("request_payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Original request payload not available for restart.")

    request = RunMarketingTeamRequest(**payload)
    _job_manager.update_job(
        job_id, status=JOB_STATUS_PENDING, error=None, progress=0, current_stage="restart_queued",
    )
    dispatch_msg = _dispatch_job(job_id, request)
    return RunMarketingTeamResponse(job_id=job_id, status="running", message=f"Job restarted. {dispatch_msg}")


# ---------------------------------------------------------------------------
# Routes — trend discovery
# ---------------------------------------------------------------------------


@app.post("/social-marketing/trends/run", response_model=TrendRunResponse)
def run_trend_discovery() -> TrendRunResponse:
    """Trigger trend discovery immediately (runs in background, returns at once)."""
    thread = threading.Thread(target=run_trend_job, daemon=True, name="trend_discovery_manual")
    thread.start()
    return TrendRunResponse(
        message="Trend discovery started. Poll GET /social-marketing/trends/latest for results."
    )


@app.get("/social-marketing/trends/latest", response_model=TrendLatestResponse)
def get_latest_trends() -> TrendLatestResponse:
    """Return the most recent trend digest. 404 if the job has not run yet."""
    digest = get_latest_digest()
    if digest is None:
        raise HTTPException(
            status_code=404,
            detail="No trend digest available yet. Trigger one via POST /social-marketing/trends/run.",
        )
    return TrendLatestResponse(digest=digest)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
