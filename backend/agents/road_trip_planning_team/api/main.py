"""FastAPI server for the Road Trip Planning team."""

from __future__ import annotations

import json
import logging
import threading
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared_graph import extract_node_text, invoke_graph_sync
from shared_observability import init_otel, instrument_fastapi_app

from ..graphs.trip_graph import build_trip_graph
from ..models import PlanTripRequest, TripItinerary
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


def _run_pipeline(trip_request: PlanTripRequest) -> TripItinerary:
    """Execute the full multi-agent planning pipeline via a Strands sequential Graph."""
    trip = trip_request.trip

    logger.info("Road trip planning started: %s → %s", trip.start_location, trip.required_stops)

    # Serialize the trip request into a task string for the graph
    task = (
        f"Plan a road trip with the following details:\n\n"
        f"Start location: {trip.start_location}\n"
        f"Required stops: {', '.join(trip.required_stops) or 'none'}\n"
        f"End location: {trip.end_location or trip.start_location}\n"
        f"Duration: {trip.trip_duration_days or 'flexible'} days\n"
        f"Vehicle: {trip.vehicle_type}\n"
        f"Budget: {trip.budget_level}\n"
        f"Preferences: {', '.join(trip.preferences) if trip.preferences else 'none'}\n\n"
        f"Travelers:\n{json.dumps([t.model_dump() for t in trip.travelers], indent=2)}"
    )

    graph = build_trip_graph()
    result = invoke_graph_sync(graph, task)

    # Extract the final itinerary from the composer node
    text = extract_node_text(result, "itinerary_composer")
    if text:
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                data = _translate_itinerary_keys(data)
                return TripItinerary.model_validate(data)
        except Exception as e:
            logger.warning("Failed to parse itinerary from graph output: %s", e)

    # Fallback minimal itinerary
    return TripItinerary(
        title=f"Road Trip: {trip.start_location} to {trip.end_location or trip.start_location}",
        overview="Itinerary generation completed but output parsing failed.",
        total_days=trip.trip_duration_days or len(trip.required_stops) * 2,
    )


def _translate_itinerary_keys(data: dict) -> dict:
    """Translate graph agent JSON keys to TripItinerary model fields.

    The graph prompt uses natural-language keys (e.g. ``summary``,
    ``date_label``, flat ``activities``) while the Pydantic model expects
    ``overview``, ``date``, and split ``morning_activities`` /
    ``afternoon_activities`` / ``evening_activities``.
    """
    # Top-level key renames
    if "summary" in data and "overview" not in data:
        data["overview"] = data.pop("summary")
    if "packing_list" in data and "packing_suggestions" not in data:
        data["packing_suggestions"] = data.pop("packing_list")
    if isinstance(data.get("route_summary"), str):
        data["route_summary"] = [data["route_summary"]]

    # Per-day translations
    for day in data.get("days") or []:
        if not isinstance(day, dict):
            continue
        if "date_label" in day and "date" not in day:
            day["date"] = day.pop("date_label")
        if "day_notes" in day and "day_summary" not in day:
            day["day_summary"] = day.pop("day_notes")

        # Flatten nested driving object into flat fields
        driving = day.pop("driving", None)
        if isinstance(driving, dict):
            if "from_location" in driving and "driving_from" not in day:
                day["driving_from"] = driving["from_location"]
            if "miles" in driving and "driving_distance_miles" not in day:
                day["driving_distance_miles"] = driving["miles"]
            if "hours" in driving and "driving_time_hours" not in day:
                day["driving_time_hours"] = driving["hours"]

        # Split flat activities list into morning/afternoon/evening
        flat_activities = day.pop("activities", None)
        if isinstance(flat_activities, list) and not (
            day.get("morning_activities") or day.get("afternoon_activities")
            or day.get("evening_activities")
        ):
            morning, afternoon, evening = [], [], []
            for act in flat_activities:
                if not isinstance(act, dict):
                    continue
                time_hint = (act.pop("time", "") or "").lower()
                if "morning" in time_hint or "breakfast" in time_hint:
                    morning.append(act)
                elif "evening" in time_hint or "dinner" in time_hint or "night" in time_hint:
                    evening.append(act)
                else:
                    afternoon.append(act)
            day["morning_activities"] = morning
            day["afternoon_activities"] = afternoon
            day["evening_activities"] = evening

        # Translate meals list into Activity-compatible dicts
        meals = day.get("meals")
        if isinstance(meals, list):
            day["meals"] = [
                {"name": m.get("venue") or m.get("name", ""), "description": m.get("notes", ""),
                 "activity_type": m.get("meal_type", "dining")}
                if isinstance(m, dict) else m
                for m in meals
            ]

        # Translate accommodation object
        acc = day.get("accommodation")
        if isinstance(acc, dict):
            if "type" in acc and "accommodation_type" not in acc:
                acc["accommodation_type"] = acc.pop("type")
            if "notes" in acc and "booking_tips" not in acc:
                acc["booking_tips"] = acc.pop("notes")

    return data


@app.get("/health")
async def health():
    """Health check for the Road Trip Planning team."""
    return {"status": "ok", "team": "road_trip_planning"}


def _run_plan_job(job_id: str, body: PlanTripRequest) -> None:
    """Background thread: run the pipeline and store result in the job store."""
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)
        itinerary = _run_pipeline(body)
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=itinerary.model_dump())
    except Exception as e:
        logger.exception("Road trip planning job %s failed", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


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
