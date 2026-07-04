"""FastAPI server for the Road Trip Planning team."""

from __future__ import annotations

import logging
import threading
from uuid import uuid4

from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared_app import create_team_app

from ..models import PlanTripRequest
from ..pipeline import run_pipeline, run_plan_background
from ..shared.job_store import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    create_job,
    get_job,
    update_job,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Re-exported for callers/tests that reference the pipeline entrypoint on this
# module (e.g. ``api.main.run_pipeline``); the canonical home is ``..pipeline``.
__all__ = ["app", "run_pipeline"]


def _startup() -> None:
    """Start the Temporal worker backstop (best-effort).

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this
    backstop covers running the app standalone (``uvicorn ...:app``).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — any failure is logged
          as a warning so it cannot abort app boot (this runs as an
          ``on_startup`` hook).
    """
    try:
        from road_trip_planning_team.temporal.worker import (
            start_road_trip_temporal_worker_thread,
        )

        start_road_trip_temporal_worker_thread()
    except Exception:
        logger.warning(
            "road_trip_planning Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )


app = create_team_app(
    service_name="road-trip-planning-team",
    team_key="road_trip_planning",
    title="Road Trip Planning API",
    description=(
        "Multi-agent road trip planner. Provide travelers, start location, required stops, "
        "and preferences — get back a full day-by-day itinerary tailored to your group."
    ),
    version="0.1.0",
    on_startup=_startup,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check for the Road Trip Planning team."""
    return {"status": "ok", "team": "road_trip_planning"}


def _dispatch_plan_run(job_id: str, body: PlanTripRequest) -> str:
    """Dispatch a run via Temporal when enabled, else a daemon thread.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - Starts exactly one execution path and returns its label
          ("Temporal" or "thread"). With ``TEMPORAL_ADDRESS`` set the run is
          started as a durable ``RoadTripWorkflow``; otherwise the legacy thread
          path runs unchanged.
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
        from road_trip_planning_team.temporal.start_workflow import start_road_trip_workflow

        start_road_trip_workflow(job_id, body.model_dump())
        logger.info("Road trip planning run dispatched via Temporal: job_id=%s", job_id)
        return "Temporal"

    thread = threading.Thread(target=run_plan_background, args=(job_id, body), daemon=True)
    thread.start()
    return "thread"


@app.post("/plan", summary="Start a road trip planning job")
async def post_plan(body: PlanTripRequest):
    """
    Submit a road trip planning job.

    Runs the full multi-agent pipeline via a Strands sequential Graph:
    1. **Traveler Profiler** — synthesizes who is going and their collective needs
    2. **Route Planner** — builds the optimal ordered route through required stops
    3. **Activities Expert** — tailors activities and dining to the group at each stop
    4. **Logistics Agent** — recommends accommodations, packing lists, and travel tips
    5. **Itinerary Composer** — assembles everything into a polished day-by-day plan

    Returns `{job_id, status}` immediately. Poll `GET /jobs/{job_id}` for progress;
    when `status` is `completed`, the full `TripItinerary` is in the `result` field.
    """
    if not body.trip.start_location:
        raise HTTPException(status_code=400, detail="start_location is required")
    if not body.trip.travelers:
        raise HTTPException(status_code=400, detail="At least one traveler is required")

    job_id = str(uuid4())
    create_job(job_id, status=JOB_STATUS_PENDING, request=body.model_dump())

    try:
        _dispatch_plan_run(job_id, body)
    except Exception as exc:
        # A dispatch failure (e.g. the Temporal worker client never connected)
        # must not leave the freshly-created job orphaned in PENDING — mark it
        # FAILED so callers polling /jobs see a terminal state.
        logger.exception("Failed to dispatch road trip planning job %s", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=f"Dispatch failed: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to start road trip planning run."
        ) from exc

    return {"job_id": job_id, "status": JOB_STATUS_PENDING}


@app.get("/jobs/{job_id}", summary="Get async job status")
async def get_job_route(job_id: str):
    """
    Get the status of an async road trip planning job.

    - `status: pending` — queued, not yet started
    - `status: running` — agents are planning
    - `status: completed` — itinerary is in the `result` field
    - `status: failed` — error details in the `error` field
    """
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return data
