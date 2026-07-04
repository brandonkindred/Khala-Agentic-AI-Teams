"""FastAPI endpoints for the market research and concept viability team."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel

from market_research_team.models import HumanReview, RunMarketResearchRequest
from market_research_team.pipeline import build_mission, run_market_research_background
from market_research_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    cancel_job,
    create_job,
    delete_job,
    get_job,
    list_jobs,
    update_job,
)
from shared_app import create_team_app

logger = logging.getLogger(__name__)


def _startup() -> None:
    """Start the Temporal worker backstop (best-effort).

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this
    backstop covers running the app standalone (``uvicorn ...:app``).

    Preconditions:
        - None (safe to call once at app startup).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — any failure is logged
          as a warning so it cannot abort app boot (this runs as an
          ``on_startup`` hook).
    """
    try:
        from market_research_team.temporal.worker import (
            start_market_research_temporal_worker_thread,
        )

        start_market_research_temporal_worker_thread()
    except Exception:
        logger.warning(
            "market_research Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )


app = create_team_app(
    service_name="market-research-team",
    team_key="market_research",
    title="Market Research Team API",
    version="1.0.0",
    description="Market research team API for competitive analysis and market insights.",
    on_startup=_startup,
)


class RunMarketResearchJobResponse(BaseModel):
    job_id: str
    status: str = JOB_STATUS_PENDING


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class JobListItem(BaseModel):
    job_id: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class JobListResponse(BaseModel):
    jobs: List[JobListItem]


def _dispatch_market_research_run(job_id: str, payload: RunMarketResearchRequest) -> str:
    """Dispatch a run via Temporal when enabled, else a daemon thread.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - Starts exactly one execution path and returns its label
          ("Temporal" or "thread"). With ``TEMPORAL_ADDRESS`` set the run is
          started as a durable ``MarketResearchWorkflow``; otherwise the
          legacy thread path runs unchanged.
        - A missing ``shared_temporal`` (Temporal not installed) falls through
          to the thread path; any *other* failure while starting the workflow
          (broken import in the team's own Temporal stack, or a client that
          never connected) propagates to the caller, which marks the job
          FAILED — a Temporal-enabled run is never silently downgraded.
    """
    try:
        from shared_temporal import is_temporal_enabled
    except ImportError:
        is_temporal_enabled = None

    if is_temporal_enabled is not None and is_temporal_enabled():
        from market_research_team.temporal.start_workflow import start_market_research_workflow

        start_market_research_workflow(job_id, payload.model_dump())
        logger.info("Market research run dispatched via Temporal: job_id=%s", job_id)
        return "Temporal"

    mission = build_mission(payload)
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    thread = threading.Thread(
        target=run_market_research_background,
        args=(job_id, mission, human_review),
        daemon=True,
    )
    thread.start()
    return "thread"


@app.post("/market-research/run", response_model=RunMarketResearchJobResponse)
def run_market_research(payload: RunMarketResearchRequest) -> RunMarketResearchJobResponse:
    """Submit a market-research run. Returns a ``job_id`` to poll for results.

    Preconditions:
        - ``payload`` is a validated ``RunMarketResearchRequest`` (FastAPI
          validates the body before this handler runs).

    Postconditions:
        - Creates a PENDING job and dispatches it (Temporal or thread),
          returning its ``job_id``.
        - On dispatch failure, marks the job FAILED and raises HTTP 500 — no
          job is ever left orphaned in PENDING.
    """
    job_id = str(uuid4())

    create_job(
        job_id,
        request=payload.model_dump(),
        product_concept=payload.product_concept,
    )

    try:
        _dispatch_market_research_run(job_id, payload)
    except Exception as exc:
        # A dispatch failure (e.g. the Temporal worker client never connected)
        # must not leave the freshly-created job orphaned in PENDING — mark it
        # FAILED so callers polling /status see a terminal state. The full
        # exception is logged; the client gets a generic 500 detail.
        logger.exception("Failed to dispatch market research job %s", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=f"Dispatch failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to start market research run.") from exc

    return RunMarketResearchJobResponse(job_id=job_id, status=JOB_STATUS_PENDING)


@app.get("/market-research/status/{job_id}", response_model=JobStatusResponse)
def get_market_research_status(job_id: str) -> JobStatusResponse:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        job_id=data.get("job_id", job_id),
        status=data.get("status", JOB_STATUS_PENDING),
        progress=data.get("progress"),
        result=data.get("result"),
        error=data.get("error"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@app.get("/market-research/jobs", response_model=JobListResponse)
def list_market_research_jobs(running_only: bool = False) -> JobListResponse:
    statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    items = [
        JobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            created_at=j.get("created_at"),
            updated_at=j.get("updated_at"),
        )
        for j in list_jobs(statuses=statuses)
    ]
    return JobListResponse(jobs=items)


@app.post("/market-research/jobs/{job_id}/cancel")
def cancel_market_research_job(job_id: str) -> Dict[str, Any]:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if cancel_job(job_id):
        return {"job_id": job_id, "status": JOB_STATUS_CANCELLED, "success": True}
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "success": False,
        "message": f"Cannot cancel job in status {data.get('status')}",
    }


@app.delete("/market-research/jobs/{job_id}")
def delete_market_research_job(job_id: str) -> Dict[str, Any]:
    if get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "deleted": True}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
