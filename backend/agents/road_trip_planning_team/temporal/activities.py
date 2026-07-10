"""Temporal activities for the Road Trip Planning team's per-step durable pipeline.

The road-trip pipeline is decomposed into fine-grained activities the durable
``RoadTripWorkflow`` drives one at a time, so a worker restart re-runs only the
unfinished specialist step instead of the whole multi-agent pipeline:

- :func:`begin_road_trip_job_activity` — mark the job RUNNING (head of the old
  ``run_plan_core``).
- :func:`profile_travelers_activity` / :func:`plan_route_activity` /
  :func:`recommend_activities_activity` / :func:`plan_logistics_activity` /
  :func:`compose_itinerary_activity` — the five specialist agents, each wrapping
  the matching neutral ``pipeline`` function.
- :func:`persist_itinerary_activity` — write the itinerary result and mark
  COMPLETED (tail of the old ``run_plan_core``).
- :func:`mark_road_trip_failed_activity` — record a FAILED job row (the
  ``run_plan_background`` except-branch).

Each activity is a plain **sync** function (run in the worker's thread-pool
executor) whose heavy imports live inside the body (or the ``_decode_*``
helpers), keeping module import — which the workflow sandbox replays during
registration — cheap and side-effect free. All payloads cross the
workflow/activity boundary as JSON-native dicts (``model_dump(mode="json")``)
and are reconstructed with pydantic inside the body.

Invariant: job-store status is written to the durable ``JobServiceClient`` store
under the ``road_trip_planning_team`` slug (the same slug the API's ``create_job``
used), so a completed run survives a worker/process restart.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

# ---------------------------------------------------------------------------
# Payload decoders — shared by the specialist activities so a change to how a
# JSON-native payload is reconstructed lives in one place. Imports stay inside
# each helper to keep module import (replayed by the workflow sandbox) clean.
# ---------------------------------------------------------------------------


def _decode_trip(request: dict[str, Any]):
    from road_trip_planning_team.models import PlanTripRequest

    return PlanTripRequest(**request).trip


def _decode_profile(profile: dict[str, Any]):
    from road_trip_planning_team.models import TravelerGroupProfile

    return TravelerGroupProfile.model_validate(profile)


def _decode_route(route: dict[str, Any]):
    from road_trip_planning_team.models import RoutePlan

    return RoutePlan.model_validate(route)


def _decode_logistics(logistics: dict[str, Any]):
    from road_trip_planning_team.models import LogisticsPlan

    return LogisticsPlan.model_validate(logistics)


@activity.defn(name="road_trip_begin_job")
def begin_road_trip_job_activity(job_id: str) -> dict[str, Any]:
    """Transition the job to RUNNING.

    Preconditions:
        - ``job_id`` refers to a job row already created by the API endpoint's
          ``create_job`` call before dispatch.

    Postconditions:
        - Sets the job-store row to RUNNING and returns ``{"job_id": job_id}``.
    """
    from road_trip_planning_team.shared.job_store import JOB_STATUS_RUNNING, update_job

    update_job(job_id, status=JOB_STATUS_RUNNING)
    return {"job_id": job_id}


@activity.defn(name="road_trip_profile_travelers")
def profile_travelers_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Run the traveler-profiler step and return its profile as a dict.

    Preconditions:
        - ``request`` is the serialized ``PlanTripRequest`` (``body.model_dump()``).

    Postconditions:
        - Returns a JSON-safe ``TravelerGroupProfile`` dict.
    """
    from road_trip_planning_team.pipeline import profile_travelers

    trip = _decode_trip(request)
    return profile_travelers(trip).model_dump(mode="json")


@activity.defn(name="road_trip_plan_route")
def plan_route_activity(request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Run the route-planner step and return the route plan as a dict.

    Preconditions:
        - ``request`` is the serialized ``PlanTripRequest``.
        - ``profile`` is a ``TravelerGroupProfile`` dict from
          :func:`profile_travelers_activity`.

    Postconditions:
        - Returns a JSON-safe ``RoutePlan`` dict.
    """
    from road_trip_planning_team.pipeline import plan_route

    return plan_route(_decode_trip(request), _decode_profile(profile)).model_dump(mode="json")


@activity.defn(name="road_trip_recommend_activities")
def recommend_activities_activity(
    request: dict[str, Any], profile: dict[str, Any], route: dict[str, Any]
) -> list[dict[str, Any]]:
    """Run the activities-expert step for every stop and return per-stop dicts.

    Preconditions:
        - ``request`` is the serialized ``PlanTripRequest``.
        - ``profile`` is a ``TravelerGroupProfile`` dict; ``route`` a ``RoutePlan``
          dict from the upstream activities.

    Postconditions:
        - Returns a list of JSON-safe ``StopActivities`` dicts, one per route stop.
          Emits a Temporal heartbeat after each stop so this per-stop LLM loop is
          detected as stalled within the heartbeat timeout, not only at
          start-to-close.
    """
    from road_trip_planning_team.pipeline import recommend_activities

    stops = recommend_activities(
        _decode_route(route),
        _decode_profile(profile),
        _decode_trip(request),
        on_stop=activity.heartbeat,
    )
    return [s.model_dump(mode="json") for s in stops]


@activity.defn(name="road_trip_plan_logistics")
def plan_logistics_activity(
    request: dict[str, Any], profile: dict[str, Any], route: dict[str, Any]
) -> dict[str, Any]:
    """Run the logistics step and return the logistics plan as a dict.

    Preconditions:
        - ``request`` is the serialized ``PlanTripRequest``.
        - ``profile`` is a ``TravelerGroupProfile`` dict; ``route`` a ``RoutePlan``
          dict from the upstream activities.

    Postconditions:
        - Returns a JSON-safe ``LogisticsPlan`` dict.
    """
    from road_trip_planning_team.pipeline import plan_logistics

    return plan_logistics(
        _decode_route(route), _decode_profile(profile), _decode_trip(request)
    ).model_dump(mode="json")


@activity.defn(name="road_trip_compose_itinerary")
def compose_itinerary_activity(
    request: dict[str, Any],
    profile: dict[str, Any],
    route: dict[str, Any],
    activities: list[dict[str, Any]],
    logistics: dict[str, Any],
) -> dict[str, Any]:
    """Assemble all specialist outputs into the final itinerary dict.

    Preconditions:
        - Every argument is the JSON-safe dict output of the corresponding
          upstream activity (``activities`` a list of ``StopActivities`` dicts).

    Postconditions:
        - Returns a JSON-safe ``TripItinerary`` dict.
    """
    from road_trip_planning_team.models import StopActivities
    from road_trip_planning_team.pipeline import compose_itinerary

    activities_per_stop = [StopActivities.model_validate(a) for a in activities]
    itinerary = compose_itinerary(
        _decode_trip(request),
        _decode_profile(profile),
        _decode_route(route),
        activities_per_stop,
        _decode_logistics(logistics),
    )
    return itinerary.model_dump(mode="json")


@activity.defn(name="road_trip_persist_itinerary")
def persist_itinerary_activity(job_id: str, itinerary: dict[str, Any]) -> dict[str, Any]:
    """Persist the itinerary result and mark the job COMPLETED.

    Preconditions:
        - ``job_id`` refers to a job already in RUNNING.
        - ``itinerary`` is the JSON-safe ``TripItinerary`` dict from
          :func:`compose_itinerary_activity`.

    Postconditions:
        - Sets the job-store row to COMPLETED with ``result=itinerary`` and
          returns ``{"job_id": job_id}``.
    """
    from road_trip_planning_team.shared.job_store import JOB_STATUS_COMPLETED, update_job

    update_job(job_id, status=JOB_STATUS_COMPLETED, result=itinerary)
    return {"job_id": job_id}


@activity.defn(name="road_trip_mark_failed")
def mark_road_trip_failed_activity(job_id: str, error: str) -> None:
    """Record a FAILED job row for a run whose workflow raised.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``error`` is the stringified failure cause.

    Postconditions:
        - Sets the job-store row to FAILED with ``error``. Idempotent — safe to
          re-run on a workflow retry.
    """
    from road_trip_planning_team.shared.job_store import JOB_STATUS_FAILED, update_job

    update_job(job_id, status=JOB_STATUS_FAILED, error=error)
