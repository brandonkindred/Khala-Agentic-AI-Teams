"""Temporal workflow + activity wrapping the startup advisor message handler."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="startup_advisor_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from startup_advisor.api.main import SendMessageRequest, send_message

    req = SendMessageRequest(**request)
    result = send_message(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="StartupAdvisorWorkflow")
class StartupAdvisorWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(minutes=30),
        )


WORKFLOWS = [StartupAdvisorWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker(
        "startup_advisor", WORKFLOWS, ACTIVITIES, task_queue="startup_advisor-queue"
    )
