"""Pipeline execution for the Road Trip Planning team.

Neutral module (no FastAPI, no Temporal) holding the per-step specialist
invocations and the job-store bookkeeping shared by the HTTP thread-dispatch
path (``api.main``) and the Temporal activities (``temporal.activities``).
Keeping it here lets the durable worker run each step without importing the
FastAPI app.

The five specialist steps are exposed as standalone typed functions
(``profile_travelers`` → ``plan_route`` → ``recommend_activities`` →
``plan_logistics`` → ``compose_itinerary``) so a Temporal workflow can drive
them one activity at a time, while ``run_pipeline`` chains the same five for
thread mode. Both modes therefore share one implementation and produce the same
``TripItinerary``.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from .agents.activities_expert_agent import ActivitiesExpertAgent
from .agents.itinerary_composer_agent import ItineraryComposerAgent
from .agents.logistics_agent import LogisticsAgent
from .agents.route_planner_agent import RoutePlannerAgent
from .agents.traveler_profiler_agent import TravelerProfilerAgent
from .models import (
    LogisticsPlan,
    PlanTripRequest,
    RoutePlan,
    StopActivities,
    TravelerGroupProfile,
    TripItinerary,
    TripRequest,
)
from .shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    update_job,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-step specialist invocations (one Temporal activity wraps each of these)
# ---------------------------------------------------------------------------


def profile_travelers(trip: TripRequest, llm=None) -> TravelerGroupProfile:
    """Synthesize a group travel profile from the trip's travelers.

    Preconditions:
        - ``trip`` is a validated ``TripRequest`` (the API layer guarantees a
          non-empty ``start_location`` and at least one traveler before dispatch).
        - ``llm`` is either ``None`` (build the default Strands agent) or a
          callable model injected for testing.

    Postconditions:
        - Returns a ``TravelerGroupProfile``. The agent degrades to a derived
          profile on LLM/parse failure rather than raising.
    """
    return TravelerProfilerAgent(llm=llm).run(trip)


def plan_route(trip: TripRequest, group_profile: TravelerGroupProfile, llm=None) -> RoutePlan:
    """Plan the optimal ordered route through the required stops.

    Preconditions:
        - ``trip`` is a validated ``TripRequest``.
        - ``group_profile`` is the ``TravelerGroupProfile`` from ``profile_travelers``.

    Postconditions:
        - Returns a ``RoutePlan`` whose ``ordered_stops`` covers the required
          stops (a minimal start→stops→end fallback on LLM/parse failure).
    """
    return RoutePlannerAgent(llm=llm).run(trip, group_profile)


def recommend_activities(
    route: RoutePlan,
    group_profile: TravelerGroupProfile,
    trip: TripRequest,
    llm=None,
    on_stop: Optional[Callable[[], None]] = None,
) -> List[StopActivities]:
    """Recommend activities and dining for each stop on the route.

    Preconditions:
        - ``route`` is the ``RoutePlan`` from ``plan_route``; ``group_profile``
          the profile from ``profile_travelers``; ``trip`` the original request.
        - ``on_stop`` is ``None`` or a no-arg callable invoked once per route
          stop (used by the Temporal activity to emit heartbeats during the
          per-stop LLM loop).

    Postconditions:
        - Returns one ``StopActivities`` per stop in ``route.ordered_stops``
          (pass-through start/end stops get an empty entry).
    """
    return ActivitiesExpertAgent(llm=llm).run(route, group_profile, trip, on_stop=on_stop)


def plan_logistics(
    route: RoutePlan,
    group_profile: TravelerGroupProfile,
    trip: TripRequest,
    llm=None,
) -> LogisticsPlan:
    """Plan accommodations, packing, and practical logistics for the trip.

    Preconditions:
        - ``route`` is the ``RoutePlan`` from ``plan_route``; ``group_profile``
          the profile from ``profile_travelers``; ``trip`` the original request.

    Postconditions:
        - Returns a ``LogisticsPlan`` (a minimal packing/tips fallback on
          LLM/parse failure).
    """
    return LogisticsAgent(llm=llm).run(route, group_profile, trip)


def compose_itinerary(
    trip: TripRequest,
    group_profile: TravelerGroupProfile,
    route: RoutePlan,
    activities_per_stop: List[StopActivities],
    logistics: LogisticsPlan,
    llm=None,
) -> TripItinerary:
    """Assemble all specialist outputs into the final day-by-day itinerary.

    Preconditions:
        - Every argument is the typed output of the corresponding upstream step.

    Postconditions:
        - Returns a fully-populated ``TripItinerary`` (a minimal fallback
          itinerary derived from the route/logistics on LLM/parse failure).
    """
    return ItineraryComposerAgent(llm=llm).run(
        trip, group_profile, route, activities_per_stop, logistics
    )


def run_pipeline(trip_request: PlanTripRequest) -> TripItinerary:
    """Execute the full multi-agent planning pipeline as five sequential steps.

    Runs the same five specialist steps a Temporal workflow drives one activity
    at a time — profiler → route → activities → logistics → composer — chained
    in-process for thread mode.

    Preconditions:
        - ``trip_request`` is a validated ``PlanTripRequest`` whose ``trip`` has
          a non-empty ``start_location`` and at least one traveler (the API
          layer enforces this before dispatch).

    Postconditions:
        - Returns a ``TripItinerary``. Each step already degrades to a typed
          fallback on LLM/parse failure; the outer guard additionally catches an
          unexpected step failure (e.g. a schema-invalid LLM response) and
          returns a minimal fallback, so thread mode always reaches a terminal
          COMPLETED state with a usable (if degraded) result rather than raising.
          (The Temporal path drives the steps as individual activities and lets a
          genuine step failure surface for durable retry/resubmission instead.)
    """
    trip = trip_request.trip

    logger.info("Road trip planning started: %s → %s", trip.start_location, trip.required_stops)

    try:
        group_profile = profile_travelers(trip)
        route = plan_route(trip, group_profile)
        activities_per_stop = recommend_activities(route, group_profile, trip)
        logistics = plan_logistics(route, group_profile, trip)
        return compose_itinerary(trip, group_profile, route, activities_per_stop, logistics)
    except Exception as e:
        logger.warning("Road trip pipeline degraded to fallback itinerary: %s", e)
        return TripItinerary(
            title=f"Road Trip: {trip.start_location} to {trip.end_location or trip.start_location}",
            overview="Itinerary generation completed but a planning step failed.",
            # max(1, ...): a trip with no explicit duration and no required stops
            # (start → end only) would otherwise evaluate to 0 days.
            total_days=max(1, trip.trip_duration_days or len(trip.required_stops) * 2),
        )


def run_plan_core(job_id: str, body: PlanTripRequest) -> None:
    """Run the pipeline with RUNNING/COMPLETED job-store bookkeeping.

    Shared by the thread dispatch path and the Temporal activity so the
    status-write order lives in one place.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``body`` is a validated ``PlanTripRequest``.

    Postconditions:
        - Writes RUNNING then COMPLETED with the itinerary result. ``run_pipeline``
          already catches step failures internally and degrades to a fallback
          ``TripItinerary`` rather than raising, so this reaches COMPLETED in the
          normal case; only a failure outside that guard (e.g. the job-store write
          itself) propagates, and the caller owns that failure policy.
    """
    update_job(job_id, status=JOB_STATUS_RUNNING)
    itinerary = run_pipeline(body)
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=itinerary.model_dump())


def run_plan_background(job_id: str, body: PlanTripRequest) -> None:
    """Thread-path runner: execute the pipeline and swallow failures as FAILED.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - On pipeline failure, marks the job FAILED and returns — a daemon
          thread has no caller to raise to.
    """
    try:
        run_plan_core(job_id, body)
    except Exception as e:
        logger.exception("Road trip planning job %s failed", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
