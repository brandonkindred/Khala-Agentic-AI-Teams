"""Temporal workflow + activity wrapping the user-agent founder workflow."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="user_agent_founder_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from user_agent_founder.orchestrator import run_workflow

    result = run_workflow(**(request or {}))
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="UserAgentFounderWorkflow")
class UserAgentFounderWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [UserAgentFounderWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker(
        "user_agent_founder", WORKFLOWS, ACTIVITIES, task_queue="user_agent_founder-queue"
    )
