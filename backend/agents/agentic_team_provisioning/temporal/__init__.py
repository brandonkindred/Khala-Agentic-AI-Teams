"""Temporal workflow + activity wrapping the agentic team provisioning handler."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="agentic_team_provisioning_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from agentic_team_provisioning.api.main import CreateTeamRequest, create_team

    req = CreateTeamRequest(**request)
    result = create_team(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="AgenticTeamProvisioningWorkflow")
class AgenticTeamProvisioningWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [AgenticTeamProvisioningWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker(
        "agentic_team_provisioning",
        WORKFLOWS,
        ACTIVITIES,
        task_queue="agentic_team_provisioning-queue",
    )
