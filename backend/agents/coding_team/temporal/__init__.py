"""Temporal workflow + activity wrapping the coding team orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="coding_team_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Run the coding-team pipeline as a Temporal activity.

    Preconditions:
        - A ``CodeEngineProvider`` is installed in THIS worker process (the
          Temporal worker does not run the ``coding_team_service`` composition
          root, so its bootstrap must call ``set_engine_provider`` itself).
    Postconditions:
        - Returns the orchestrator result as a dict, or raises with an
          actionable message when the worker is mis-wired — instead of failing
          later, mid-run, with a generic worker-build error.
    """
    from coding_team.api.main import RunRequest
    from coding_team.engine_provider import get_engine_provider
    from coding_team.orchestrator import run_coding_team_orchestrator

    if get_engine_provider() is None:
        raise RuntimeError(
            "coding_team Temporal worker has no CodeEngineProvider installed: this worker "
            "process never ran the coding_team_service composition root. Call "
            "coding_team.engine_provider.set_engine_provider(...) in the worker bootstrap "
            "before executing CodingTeamWorkflow."
        )
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
