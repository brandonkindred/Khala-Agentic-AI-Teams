"""Temporal workflow + activity for the sales_team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without
executing any non-deterministic top-level code (e.g. ``os.getenv``,
worker bootstrap). Co-locating ``start_team_worker``/``is_temporal_enabled``
with the workflow class trips the sandbox with
``__call__ on os.getenv restricted`` during workflow registration.

The activity delegates to ``sales_team.job_runner.run_pipeline_job`` so the
job-store bookkeeping (RUNNING → COMPLETED or FAILED) lives in exactly one
place, shared with the thread-dispatch path in ``sales_team.api.main``.
``job_runner`` has no import of ``sales_team.api.main`` (no FastAPI app
creation, stale-job monitor, or invoke shim), so this activity stays cheap
to import even if a worker process never imports the API module. Status is
written to the durable ``JobServiceClient`` store, so a completed run
survives a worker/process restart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn(name="sales_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the request, run the sales pipeline orchestrator, and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store
          (the API endpoint calls ``_job_manager.create_job`` before dispatch).
        - ``request`` is the serialized ``SalesPipelineRequest``
          (i.e. ``payload.model_dump(mode="json")``).

    Postconditions:
        - On success the job store row ends in ``COMPLETED`` with the
          orchestrator result and the activity returns ``{"job_id": job_id}``.
        - If ``request`` fails to validate as a ``SalesPipelineRequest``
          (e.g. missing required fields), the job store row is marked
          ``FAILED`` here — before ``run_pipeline_job`` would otherwise ever
          be reached — so the job never sits stuck in PENDING.
        - On any other genuine failure, the job store row ends in ``FAILED``
          (via ``run_pipeline_job``'s own exception handling) and this
          activity re-raises so the failure surfaces as a failed Temporal
          workflow rather than a silently-"completed" one. Auto-retry is
          bounded by the workflow's ``RetryPolicy`` (see ``SalesWorkflow.run``).
    """
    from job_service_client import JOB_STATUS_FAILED
    from sales_team.job_runner import job_manager, run_pipeline_job
    from sales_team.models import SalesPipelineRequest

    try:
        pipeline_request = SalesPipelineRequest(**request)
    except Exception as exc:
        # Best-effort mark FAILED, but the original validation error must
        # always be what propagates — a failing update_job (e.g. transient
        # job-service outage) should not mask it, or Temporal would surface a
        # misleading error and the real cause would be lost.
        try:
            job_manager.update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))
        except Exception:
            activity.logger.exception(
                "Failed to mark job %s FAILED after request validation error", job_id
            )
        raise

    run_pipeline_job(job_id, pipeline_request)
    job = job_manager.get_job(job_id)
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
