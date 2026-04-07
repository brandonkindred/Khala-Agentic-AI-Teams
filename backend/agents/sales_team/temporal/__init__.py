"""Temporal workflow + activity wrapping the sales pod orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="sales_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from sales_team.api.main import SalesPipelineRequest
    from sales_team.orchestrator import SalesPodOrchestrator

    req = SalesPipelineRequest(**request)
    result = SalesPodOrchestrator().run(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="SalesWorkflow")
class SalesWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [SalesWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("sales", WORKFLOWS, ACTIVITIES, task_queue="sales-queue")
