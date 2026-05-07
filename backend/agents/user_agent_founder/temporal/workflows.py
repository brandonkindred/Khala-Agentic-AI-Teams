"""Temporal workflow + activity for the user_agent_founder team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without
executing any non-deterministic top-level code (e.g. ``os.getenv``,
worker bootstrap). The previous layout co-located ``start_team_worker``
with the workflow class, which the sandbox aborted with
``__call__ on os.getenv restricted``.
"""

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
