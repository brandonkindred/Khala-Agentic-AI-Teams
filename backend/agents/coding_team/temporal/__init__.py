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
          no plan has nothing to run). It may optionally carry a ``job_id`` (the
          row the API already created for the client to poll).
    Postconditions:
        - Runs the orchestrator wired to the job store against the request's
          ``job_id`` when supplied (the API created the row; do not create it
          again), or mints one and creates the row when absent. Returns the
          final job snapshot as a dict. Raises with an actionable message when
          the worker is mis-wired (no provider) or the request carries no plan —
          instead of failing later, mid-run, with a generic error.
    """
    import uuid

    from coding_team.api.main import (
        RunRequest,
        create_job,
        get_job,
        plan_from_input,
        run_orchestrator_wired,
    )
    from coding_team.engine_provider import get_engine_provider

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
    # Reuse the API-created job row when the dispatcher supplied its id, so the
    # id the client polls is the id the orchestrator writes to. Only mint + create
    # a row when dispatched without one (self-contained callers/tests). Either way
    # run through the shared orchestrator wiring — the same path POST /run uses —
    # against the real (job_id, repo_path, plan) signature.
    supplied_job_id = request.get("job_id")
    job_id = supplied_job_id or str(uuid.uuid4())
    if not supplied_job_id:
        create_job(job_id=job_id, repo_path=req.repo_path, plan_input=req.plan_input)
    plan = plan_from_input(req.plan_input, req.repo_path)
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

# NB: no worker self-boot at import time. This module DEFINES CodingTeamWorkflow,
# so the temporalio sandbox re-imports it during workflow registration; a top-level
# ``is_temporal_enabled()`` call (os.getenv) would trip the sandbox, and an
# import-time ``start_team_worker`` races the first dispatch. Boot lives in
# ``coding_team.temporal.worker`` (invoked per uvicorn worker by team_service).
