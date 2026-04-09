"""FastAPI server for the Road Trip Planning team."""

from __future__ import annotations

import logging
import threading
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from llm_service import get_client
from shared_observability import init_otel, instrument_fastapi_app

from ..agents.activities_expert_agent import ActivitiesExpertAgent
from ..agents.itinerary_composer_agent import ItineraryComposerAgent
from ..agents.logistics_agent import LogisticsAgent
from ..agents.route_planner_agent import RoutePlannerAgent
from ..agents.traveler_profiler_agent import TravelerProfilerAgent
from ..models import PlanTripRequest, PlanTripResponse, TripItinerary
from ..shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    update_job,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

init_otel(service_name="road-trip-planning-team", team_key="road_trip_planning")

app = FastAPI(
    title="Road Trip Planning API",
    description=(
        "Multi-agent road trip planner. Provide travelers, start location, required stops, "
        "and preferences — get back a full day-by-day itinerary tailored to your group."
    ),
    version="0.1.0",
)
instrument_fastapi_app(app, team_key="road_trip_planning")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialise LLM client and all specialist agents once at startup
_llm = get_client("road_trip_planning")
_traveler_profiler = TravelerProfilerAgent(_llm)
_route_planner = RoutePlannerAgent(_llm)
_activities_expert = ActivitiesExpertAgent(_llm)
_logistics_agent = LogisticsAgent(_llm)
_itinerary_composer = ItineraryComposerAgent(_llm)


def _run_pipeline(trip_request: PlanTripRequest) -> TripItinerary:
    """Execute the full multi-agent planning pipeline synchronously."""
    trip = trip_request.trip

    logger.info("Road trip planning started: %s → %s", trip.start_location, trip.required_stops)

    # Phase 1 — Traveler Profiler: understand who is going and what they need
    logger.info("Phase 1: Traveler profiling")
    group_profile = _traveler_profiler.run(trip)

    # Phase 2 — Route Planner: build the optimal route
    logger.info("Phase 2: Route planning")
    route = _route_planner.run(trip, group_profile)

    # Phase 3 — Activities Expert: tailor activities for each stop
    logger.info("Phase 3: Activities recommendation (%d stops)", len(route.ordered_stops))
    activities_per_stop = _activities_expert.run(route, group_profile, trip)

    # Phase 4 — Logistics Agent: accommodations, packing, tips
    logger.info("Phase 4: Logistics planning")
    logistics = _logistics_agent.run(route, group_profile, trip)

    # Phase 5 — Itinerary Composer: assemble the final itinerary
    logger.info("Phase 5: Composing itinerary")
    itinerary = _itinerary_composer.run(trip, group_profile, route, activities_per_stop, logistics)

    logger.info("Road trip planning complete: %s (%d days)", itinerary.title, itinerary.total_days)
    return itinerary


@app.get("/health")
async def health():
    """Health check for the Road Trip Planning team."""
    return {"status": "ok", "team": "road_trip_planning"}


@app.post("/plan", response_model=PlanTripResponse, summary="Plan a road trip itinerary")
async def post_plan(body: PlanTripRequest) -> PlanTripResponse:
    """
    Plan a complete road trip itinerary (synchronous).

    Runs the full multi-agent pipeline:
    1. **Traveler Profiler** — synthesizes who is going and their collective needs
    2. **Route Planner** — builds the optimal ordered route through required stops
    3. **Activities Expert** — tailors activities and dining to the group at each stop
    4. **Logistics Agent** — recommends accommodations, packing lists, and travel tips
    5. **Itinerary Composer** — assembles everything into a polished day-by-day plan

    Returns a complete `TripItinerary` with day-by-day plans, activities, meals, and accommodations.
    """
    if not body.trip.start_location:
        raise HTTPException(status_code=400, detail="start_location is required")
    if not body.trip.travelers:
        raise HTTPException(status_code=400, detail="At least one traveler is required")

    try:
        itinerary = _run_pipeline(body)
    except Exception as e:
        logger.exception("Road trip planning pipeline failed")
        raise HTTPException(status_code=500, detail=f"Planning failed: {e}") from e

    return PlanTripResponse(itinerary=itinerary)


def _run_plan_job(job_id: str, body: PlanTripRequest) -> None:
    """Background thread: run the pipeline and store result in the job store."""
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)
        itinerary = _run_pipeline(body)
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=itinerary.model_dump())
    except Exception as e:
        logger.exception("Road trip planning job %s failed", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


@app.post("/plan/async", summary="Start async road trip planning")
async def post_plan_async(body: PlanTripRequest):
    """
    Start road trip planning as a background job (asynchronous).

    Returns a `job_id` immediately. Poll `GET /jobs/{job_id}` to check status.
    When `status` is `completed`, the full itinerary is in the `result` field.
    """
    if not body.trip.start_location:
        raise HTTPException(status_code=400, detail="start_location is required")
    if not body.trip.travelers:
        raise HTTPException(status_code=400, detail="At least one traveler is required")

    job_id = str(uuid4())
    create_job(job_id, status=JOB_STATUS_PENDING, request=body.model_dump())
    thread = threading.Thread(target=_run_plan_job, args=(job_id, body), daemon=True)
    thread.start()
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
