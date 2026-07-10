"""Temporal workflow + per-step activities wrapping the road-trip pipeline.

The durable ``RoadTripWorkflow`` (in :mod:`workflows`) drives the eight
activities (in :mod:`activities`) one at a time: begin → the five specialist
steps → persist, with mark-failed on error. Both modules are sandbox-safe — no
top-level non-deterministic calls. Worker startup lives in :mod:`worker` and is
invoked by the team_service entrypoint at boot (``TEAM_TEMPORAL_WORKER_MODULE``
/ ``TEAM_TEMPORAL_WORKER_FUNC``), with the API lifespan as a standalone-dev
backstop, so the Temporal client is connected before the API serves its first
request. This package ``__init__`` must stay free of import-time side effects
(no worker boot, no ``os.getenv``) — the temporalio sandbox replays it during
workflow registration.
"""

from __future__ import annotations

from road_trip_planning_team.temporal.activities import (
    begin_road_trip_job_activity,
    compose_itinerary_activity,
    mark_road_trip_failed_activity,
    persist_itinerary_activity,
    plan_logistics_activity,
    plan_route_activity,
    profile_travelers_activity,
    recommend_activities_activity,
)
from road_trip_planning_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from road_trip_planning_team.temporal.workflows import RoadTripWorkflow

WORKFLOWS = [RoadTripWorkflow]
ACTIVITIES = [
    begin_road_trip_job_activity,
    profile_travelers_activity,
    plan_route_activity,
    recommend_activities_activity,
    plan_logistics_activity,
    compose_itinerary_activity,
    persist_itinerary_activity,
    mark_road_trip_failed_activity,
]

__all__ = [
    "ACTIVITIES",
    "RoadTripWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "begin_road_trip_job_activity",
    "compose_itinerary_activity",
    "mark_road_trip_failed_activity",
    "persist_itinerary_activity",
    "plan_logistics_activity",
    "plan_route_activity",
    "profile_travelers_activity",
    "recommend_activities_activity",
]
