"""Temporal workflow + activity for the sales_team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without
executing any non-deterministic top-level code (e.g. ``os.getenv``,
worker bootstrap). Co-locating ``start_team_worker``/``is_temporal_enabled``
with the workflow class trips the sandbox with
``__call__ on os.getenv restricted`` during workflow registration.

The activity delegates to the *existing* ``_run_pipeline_job`` (defined in
``sales_team.api.main``) so the job-store bookkeeping (RUNNING → COMPLETED
or FAILED) lives in exactly one place, shared with the thread-dispatch path.
Status is written to the durable ``JobServiceClient`` store, so a completed
run survives a worker/process restart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn(name="sales_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the sales pipeline orchestrator and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store
          (the API endpoint calls ``_job_manager.create_job`` before dispatch).
        - ``request`` is the serialized ``SalesPipelineRequest``
          (i.e. ``payload.model_dump(mode="json")``).

    Postconditions:
        - On success the job store row ends in ``COMPLETED`` with the
          orchestrator result and the activity returns ``{"job_id": job_id}``.
        - On a genuine failure, the job store row ends in ``FAILED`` (via
          ``_run_pipeline_job``'s own exception handling) and this activity
          re-raises so the failure surfaces as a failed Temporal workflow
          rather than a silently-"completed" one. Auto-retry is bounded by
          the workflow's ``RetryPolicy`` (see ``SalesWorkflow.run``).
    """
    from sales_team.api.main import JOB_STATUS_FAILED, _job_manager, _run_pipeline_job
    from sales_team.models import SalesPipelineRequest

    _run_pipeline_job(job_id, SalesPipelineRequest(**request))
    job = _job_manager.get_job(job_id)
    if job and job.get("status") == JOB_STATUS_FAILED:
        raise RuntimeError(job.get("error") or "Sales pipeline failed")
    return {"job_id": job_id}


@workflow.defn(name="SalesWorkflow")
class SalesWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the sales pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``SalesPipelineRequest``
              (``payload.model_dump(mode="json")``).

        Postconditions:
            - Delegates to ``run_pipeline_activity`` (which owns job-store
              status bookkeeping) and returns its ``{"job_id": job_id}`` result.
        """
        return await workflow.execute_activity(
            run_pipeline_activity,
            args=[job_id, request],
            start_to_close_timeout=timedelta(hours=2),
            # The orchestrator is a long, non-idempotent LLM pipeline. Cap at a
            # single attempt: a failure surfaces as a failed workflow + a FAILED
            # job-store row for explicit resubmission rather than being
            # auto-retried. A worker crash mid-activity leaves an orphaned
            # RUNNING job that team_service's startup/shutdown recovery
            # reconciles to "interrupted" instead of silently re-running the
            # expensive pipeline.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
