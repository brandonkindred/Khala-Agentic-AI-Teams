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
        - ``request`` is a serialized ``RunRequest`` carrying a non-null
          ``plan_input`` (the workflow executes a plan; a job-only request with
          no plan has nothing to run).
    Postconditions:
        - Creates a job, runs the orchestrator against it wired to the job
          store, and returns the final job snapshot as a dict. Raises with an
          actionable message when the worker is mis-wired (no provider) or the
          request carries no plan — instead of failing later, mid-run, with a
          generic error.
    """
    import uuid

    from coding_team.api.main import (
        RunRequest,
        create_job,
        get_job,
        run_orchestrator_wired,
    )
    from coding_team.engine_provider import get_engine_provider
    from coding_team.models import CodingTeamPlanInput

    if get_engine_provider() is None:
        raise RuntimeError(
            "coding_team Temporal worker has no CodeEngineProvider installed: this worker "
            "process never ran the coding_team_service composition root. Call "
            "coding_team.engine_provider.set_engine_provider(...) in the worker bootstrap "
            "before executing CodingTeamWorkflow."
        )
    req = RunRequest(**request)
    if not req.plan_input:
        raise ValueError(
            "CodingTeamWorkflow requires a plan_input to execute; received a job-only request "
            "with no plan."
        )
    # Mint a job and run it through the shared orchestrator wiring — the same path
    # POST /run uses — against the real (job_id, repo_path, plan) signature.
    job_id = str(uuid.uuid4())
    create_job(job_id=job_id, repo_path=req.repo_path, plan_input=req.plan_input)
    plan = CodingTeamPlanInput.model_validate({**req.plan_input, "repo_path": req.repo_path})
    run_orchestrator_wired(job_id, req.repo_path, plan)
    return get_job(job_id) or {"job_id": job_id, "status": "unknown"}


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
