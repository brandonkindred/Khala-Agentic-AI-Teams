"""FastAPI endpoints for running and monitoring the social media marketing team."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
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

from blog_research_agent.tools.web_search import OllamaWebSearch
from llm_service import get_client
from social_media_marketing_team.models import (
    BrandGoals,
    CampaignPerformanceSnapshot,
    HumanReview,
    PostPerformanceObservation,
    TeamOutput,
)
from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator
from social_media_marketing_team.trend_discovery_agent import TrendDiscoveryAgent
from social_media_marketing_team.trend_models import TrendDigest

app = FastAPI(title="Social Media Marketing Team API", version="1.0.0")

logger = logging.getLogger(__name__)
_job_manager = CentralJobManager(team="social_media_marketing_team")
_stale_monitor_stop = start_stale_job_monitor(
    _job_manager,
    interval_seconds=15.0,
    stale_after_seconds=300.0,
    reason="Job heartbeat stale while pending/running",
)

# Latest trend digest — updated by the daily cron and the manual trigger endpoint.
# Each worker process maintains its own copy; for multi-worker deployments the last
# worker to complete will serve the most recent digest.
_latest_digest: Optional[TrendDigest] = None
_scheduler: Optional[BackgroundScheduler] = None


class RunMarketingTeamRequest(BaseModel):
    brand_guidelines_path: str = Field(..., max_length=4096, description="Path to brand guidelines document")
    brand_objectives_path: str = Field(..., max_length=4096, description="Path to brand objectives document")
    llm_model_name: str = Field(..., max_length=256, description="Name of local LLM model to use")
    brand_name: str = Field(default="Brand", max_length=256)
    target_audience: str = Field(default="general audience", max_length=5000)
    goals: List[str] = Field(default_factory=lambda: ["engagement", "follower growth"])
    voice_and_tone: str = Field(default="professional, clear, and human", max_length=5000)
    cadence_posts_per_day: int = Field(default=2, ge=1)
    duration_days: int = Field(default=14, ge=1)
    human_approved_for_testing: bool = Field(default=False)
    human_feedback: str = Field(default="", max_length=50_000)


class ReviseMarketingTeamRequest(BaseModel):
    feedback: str = Field(..., min_length=3)
    approved_for_testing: bool = Field(default=False)


class RunMarketingTeamResponse(BaseModel):
    job_id: str
    status: str
    message: str


class MarketingJobStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    llm_model_name: str
    brand_guidelines_path: str
    brand_objectives_path: str
    last_updated_at: str
    eta_hint: Optional[str] = None
    error: Optional[str] = None
    result: Optional[TeamOutput] = None


class MarketingJobListItem(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    created_at: Optional[str] = None
    last_updated_at: Optional[str] = None


class PerformanceIngestRequest(BaseModel):
    observations: List[PostPerformanceObservation] = Field(default_factory=list)


class PerformanceIngestResponse(BaseModel):
    job_id: str
    campaign_name: Optional[str] = None
    observations_ingested: int
    message: str


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _read_text_file(path: str) -> str:
    fp = Path(path).expanduser().resolve()
    if not fp.is_file():
        raise ValueError(f"File not found: {path}")
    return fp.read_text(encoding="utf-8", errors="replace")


def _update_job(job_id: str, **fields) -> None:
    _job_manager.update_job(job_id, **fields)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all pending or running marketing jobs as failed (e.g. on server shutdown)."""
    _job_manager.mark_stale_active_jobs_failed(stale_after_seconds=0, reason=reason)


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

        _update_job(job_id, current_stage="building_campaign_proposal", progress=50, eta_hint="~1 minute")
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
        _update_job(job_id, status=JOB_STATUS_FAILED, current_stage="failed", error=str(exc), eta_hint=None)


@app.post("/social-marketing/run", response_model=RunMarketingTeamResponse)
def run_marketing_team(request: RunMarketingTeamRequest) -> RunMarketingTeamResponse:
    for p in (request.brand_guidelines_path, request.brand_objectives_path):
        if not Path(p).expanduser().resolve().is_file():
            raise HTTPException(status_code=400, detail=f"File not found: {p}")

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

    try:
        from social_media_marketing_team.temporal.client import is_temporal_enabled
        from social_media_marketing_team.temporal.start_workflow import start_team_job_workflow
        if is_temporal_enabled():
            start_team_job_workflow(job_id, request.model_dump())
            return RunMarketingTeamResponse(
                job_id=job_id,
                status="running",
                message=f"Social marketing team started (Temporal). Poll GET /social-marketing/status/{job_id} for updates.",
            )
    except ImportError:
        pass

    thread = threading.Thread(target=_run_team_job, args=(job_id, request), daemon=True)
    thread.start()

    return RunMarketingTeamResponse(
        job_id=job_id,
        status="running",
        message=f"Social marketing team started. Poll GET /social-marketing/status/{job_id} for updates.",
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
    if job.get("result") and getattr(job["result"], "proposal", None):
        campaign_name = job["result"].proposal.campaign_name

    return PerformanceIngestResponse(
        job_id=job_id,
        campaign_name=campaign_name,
        observations_ingested=len(payload.observations),
        message="Performance observations stored.",
    )


@app.post("/social-marketing/revise/{job_id}", response_model=RunMarketingTeamResponse)
def revise_marketing_team(job_id: str, request: ReviseMarketingTeamRequest) -> RunMarketingTeamResponse:
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    original_payload = job.get("request_payload")
    if not isinstance(original_payload, dict):
        raise HTTPException(status_code=400, detail="Original run payload not available for revision")

    original_request = RunMarketingTeamRequest(**original_payload)
    revised = original_request.model_copy(update={
        "human_feedback": request.feedback,
        "human_approved_for_testing": request.approved_for_testing,
    })
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

    try:
        from social_media_marketing_team.temporal.client import is_temporal_enabled
        from social_media_marketing_team.temporal.start_workflow import start_team_job_workflow
        if is_temporal_enabled():
            start_team_job_workflow(job_id, revised.model_dump())
            return RunMarketingTeamResponse(
                job_id=job_id,
                status="running",
                message=f"Revision started for {job_id} (Temporal). Poll GET /social-marketing/status/{job_id} for updates.",
            )
    except ImportError:
        pass

    thread = threading.Thread(target=_run_team_job, args=(job_id, revised), daemon=True)
    thread.start()
    return RunMarketingTeamResponse(
        job_id=job_id,
        status="running",
        message=f"Revision started for {job_id}. Poll GET /social-marketing/status/{job_id} for updates.",
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
    return MarketingJobStatusResponse(**{k: v for k, v in job.items() if k in MarketingJobStatusResponse.model_fields})


class CancelMarketingJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteMarketingJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


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


def _run_trend_job() -> None:
    """Run trend discovery and update _latest_digest. Safe to call from any thread."""
    global _latest_digest
    logger.info("TrendDiscovery: starting run")
    try:
        llm = get_client("trend_discovery")
        searcher = OllamaWebSearch()
        agent = TrendDiscoveryAgent(llm_client=llm, web_search=searcher)
        digest = agent.run()
        _latest_digest = digest
        logger.info(
            "TrendDiscovery: completed — %d topics, generated_at=%s",
            len(digest.topics),
            digest.generated_at,
        )
    except Exception as exc:
        logger.error("TrendDiscovery: run failed: %s", exc, exc_info=True)


@app.on_event("startup")
def _start_scheduler() -> None:
    global _scheduler
    et = pytz.timezone("America/New_York")
    _scheduler = BackgroundScheduler(timezone=et)
    _scheduler.add_job(
        _run_trend_job,
        CronTrigger(hour=8, minute=0, timezone=et),
        id="trend_discovery_daily",
        name="Daily social media trend discovery",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("TrendDiscovery: scheduler started — daily job at 08:00 America/New_York")


@app.on_event("shutdown")
def _stop_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("TrendDiscovery: scheduler stopped")


class TrendRunResponse(BaseModel):
    message: str


class TrendLatestResponse(BaseModel):
    digest: TrendDigest


@app.post("/social-marketing/trends/run", response_model=TrendRunResponse)
def run_trend_discovery() -> TrendRunResponse:
    """Trigger trend discovery immediately (runs in background, returns at once)."""
    thread = threading.Thread(target=_run_trend_job, daemon=True, name="trend_discovery_manual")
    thread.start()
    return TrendRunResponse(message="Trend discovery started. Poll GET /social-marketing/trends/latest for results.")


@app.get("/social-marketing/trends/latest", response_model=TrendLatestResponse)
def get_latest_trends() -> TrendLatestResponse:
    """Return the most recent trend digest. 404 if the job has not run yet."""
    if _latest_digest is None:
        raise HTTPException(
            status_code=404,
            detail="No trend digest available yet. Trigger one via POST /social-marketing/trends/run.",
        )
    return TrendLatestResponse(digest=_latest_digest)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
