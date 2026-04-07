"""Temporal workflow + activity wrapping the investment team orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="investment_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from investment_team.api.main import CreateProposalRequest
    from investment_team.orchestrator import InvestmentTeamOrchestrator

    req = CreateProposalRequest(**request)
    result = InvestmentTeamOrchestrator().run_web_action(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="InvestmentWorkflow")
class InvestmentWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [InvestmentWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("investment", WORKFLOWS, ACTIVITIES, task_queue="investment-queue")
