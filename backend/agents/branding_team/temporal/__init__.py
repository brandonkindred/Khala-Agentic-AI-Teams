"""Temporal workflow + activity wrapping the branding team orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="branding_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from branding_team.api.main import RunBrandingTeamRequest
    from branding_team.orchestrator import BrandingTeamOrchestrator

    req = RunBrandingTeamRequest(**request)
    result = BrandingTeamOrchestrator().run(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="BrandingWorkflow")
class BrandingWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [BrandingWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("branding", WORKFLOWS, ACTIVITIES, task_queue="branding-queue")
