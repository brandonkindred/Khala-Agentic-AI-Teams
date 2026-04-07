"""Temporal workflow + activity wrapping the road trip planning pipeline."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="road_trip_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from road_trip_planning_team.api.main import PlanTripRequest, _run_pipeline

    req = PlanTripRequest(**request)
    result = _run_pipeline(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="RoadTripWorkflow")
class RoadTripWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [RoadTripWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker(
        "road_trip_planning", WORKFLOWS, ACTIVITIES, task_queue="road_trip_planning-queue"
    )
