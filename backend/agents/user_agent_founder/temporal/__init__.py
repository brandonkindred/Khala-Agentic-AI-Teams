"""Temporal workflow + activity wrapping the user-agent founder workflow."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="user_agent_founder_run_pipeline")
def run_pipeline_activity(run_id: str) -> dict[str, Any]:
    """Execute the founder workflow for ``run_id``.

    Reconstructs the store + agent inside the activity because neither is
    serialisable across the Temporal boundary. The activity is idempotent
    from the orchestrator's perspective — ``run_workflow`` internally
    updates both the founder store and the centralized job service on
    every phase transition and on failure.
    """
    from user_agent_founder.agent import FounderAgent
    from user_agent_founder.orchestrator import run_workflow
    from user_agent_founder.store import get_founder_store

    store = get_founder_store()
    agent = FounderAgent()
    # run_workflow resolves the adapter from the run row's target_team_key
    # when none is supplied — keeps this boundary thin.
    run_workflow(run_id, store, agent)
    return {"run_id": run_id}


@workflow.defn(name="UserAgentFounderWorkflow")
class UserAgentFounderWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            run_id,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [UserAgentFounderWorkflow]
ACTIVITIES = [run_pipeline_activity]
TASK_QUEUE = "user_agent_founder-queue"
WORKFLOW_ID_PREFIX = "user-agent-founder-"

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("user_agent_founder", WORKFLOWS, ACTIVITIES, task_queue=TASK_QUEUE)
