"""FastAPI endpoints for running and monitoring the social media marketing team."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException

from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
    start_stale_job_monitor,
)
from social_media_marketing_team.models import (
    BrandGoals,
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
# Allowed base directories for brand document file reads.  Paths outside these
# roots will be rejected to prevent path-traversal attacks.
# ---------------------------------------------------------------------------
_ALLOWED_FILE_ROOTS: List[Path] = []
_agent_cache = os.getenv("AGENT_CACHE", "")
if _agent_cache:
    _ALLOWED_FILE_ROOTS.append(Path(_agent_cache).resolve())
_data_dir = Path("/data")
if _data_dir.is_dir():
    _ALLOWED_FILE_ROOTS.append(_data_dir.resolve())
_tmp_dir = Path("/tmp")
if _tmp_dir.is_dir():
    _ALLOWED_FILE_ROOTS.append(_tmp_dir.resolve())

# ---------------------------------------------------------------------------
# App & lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_application: FastAPI) -> AsyncIterator[None]:
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Social Media Marketing Team API", version="1.0.0", lifespan=_lifespan)

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


def _validate_file_path(path: str) -> Path:
    """Resolve *path* and ensure it exists inside an allowed root directory.

    Raises ``ValueError`` when the path does not exist, or lies outside every
    configured allowed root.  When no roots are configured, **all** reads are
    denied to prevent silent bypass of the protection in environments that
    forgot to set ``AGENT_CACHE``.
    """
    fp = Path(path).expanduser().resolve()
    if not fp.is_file():
        raise ValueError(f"File not found: {path}")
    if not _ALLOWED_FILE_ROOTS:
        raise ValueError(
            f"Access denied: no allowed file roots configured (set AGENT_CACHE or place files under /data or /tmp). "
            f"Requested path: {path}"
        )
    if not any(fp == root or root in fp.parents for root in _ALLOWED_FILE_ROOTS):
        raise ValueError(
            f"Access denied: {path} is outside allowed directories. "
            f"Allowed roots: {[str(r) for r in _ALLOWED_FILE_ROOTS]}"
        )
    return fp


def _read_text_file(path: str) -> str:
    fp = _validate_file_path(path)
    return fp.read_text(encoding="utf-8", errors="replace")


def _update_job(job_id: str, **fields) -> None:
    _job_manager.update_job(job_id, **fields)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all pending or running marketing jobs as failed (e.g. on server shutdown)."""
    _job_manager.mark_stale_active_jobs_failed(stale_after_seconds=0, reason=reason)


# ---------------------------------------------------------------------------
# Background job execution
# ---------------------------------------------------------------------------


def _run_team_job(job_id: str, request: RunMarketingTeamRequest) -> None:
    try:
        _update_job(
            job_id,
            status="running",
            current_stage="loading_brand_documents",
            progress=20,
            eta_hint="~1-2 minutes",
        )
        guidelines_text = _read_text_file(request.brand_guidelines_path)
        objectives_text = _read_text_file(request.brand_objectives_path)

        _update_job(
            job_id, current_stage="building_campaign_proposal", progress=50, eta_hint="~1 minute"
        )
        orchestrator = SocialMediaMarketingOrchestrator(llm_model_name=request.llm_model_name)
        goals = BrandGoals(
            brand_name=request.brand_name,
            target_audience=request.target_audience,
            goals=request.goals,
            voice_and_tone=request.voice_and_tone,
            cadence_posts_per_day=request.cadence_posts_per_day,
            duration_days=request.duration_days,
            brand_guidelines_path=request.brand_guidelines_path,
            brand_objectives_path=request.brand_objectives_path,
            brand_guidelines=guidelines_text,
            brand_objectives=objectives_text,
        )

        _update_job(
            job_id,
            current_stage="running_collaboration_and_planning",
            progress=75,
            eta_hint="~30-60 seconds",
        )
        performance = CampaignPerformanceSnapshot(
            campaign_name=f"{request.brand_name} multi-platform growth sprint",
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


def _dispatch_job(job_id: str, request: RunMarketingTeamRequest) -> str:
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

    thread = threading.Thread(target=_run_team_job, args=(job_id, request), daemon=True)
    thread.start()
    return f"Poll GET /social-marketing/status/{job_id} for updates."


# ---------------------------------------------------------------------------
# Routes — campaign jobs
# ---------------------------------------------------------------------------


@app.post("/social-marketing/run", response_model=RunMarketingTeamResponse)
def run_marketing_team(request: RunMarketingTeamRequest) -> RunMarketingTeamResponse:
    for p in (request.brand_guidelines_path, request.brand_objectives_path):
        try:
            _validate_file_path(p)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = str(uuid.uuid4())
    now = _now()
    _job_manager.create_job(
        job_id,
        job_type="run_marketing_team",
        status=JOB_STATUS_PENDING,
        current_stage="queued",
        progress=0,
        llm_model_name=request.llm_model_name,
        brand_guidelines_path=request.brand_guidelines_path,
        brand_objectives_path=request.brand_objectives_path,
        result=None,
        error=None,
        eta_hint="queued",
        performance_observations=[],
        created_at=now,
        last_updated_at=now,
        revision_history=[],
        request_payload=request.model_dump(),
    )

    dispatch_msg = _dispatch_job(job_id, request)
    return RunMarketingTeamResponse(
        job_id=job_id,
        status="running",
        message=f"Social marketing team started. {dispatch_msg}",
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

    dispatch_msg = _dispatch_job(job_id, revised)
    return RunMarketingTeamResponse(
        job_id=job_id,
        status="running",
        message=f"Revision started for {job_id}. {dispatch_msg}",
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
    return MarketingJobStatusResponse(
        **{k: v for k, v in job.items() if k in MarketingJobStatusResponse.model_fields}
    )


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
