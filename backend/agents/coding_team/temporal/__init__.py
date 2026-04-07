"""Temporal workflow + activity wrapping the coding team orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="coding_team_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from coding_team.api.main import RunRequest
    from coding_team.orchestrator import run_coding_team_orchestrator

    req = RunRequest(**request)
    result = run_coding_team_orchestrator(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="CodingTeamWorkflow")
class CodingTeamWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=4),
        )


WORKFLOWS = [CodingTeamWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("coding_team", WORKFLOWS, ACTIVITIES, task_queue="coding_team-queue")
