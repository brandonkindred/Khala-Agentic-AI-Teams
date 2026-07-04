"""Temporal workflow + activity for the Road Trip Planning team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without executing
any non-deterministic top-level code (e.g. ``os.getenv``, worker bootstrap).
Co-locating ``start_team_worker``/``is_temporal_enabled`` with the workflow
class trips the sandbox with ``__call__ on os.getenv restricted`` during
workflow registration.

The activity reuses the shared pipeline core (``run_plan_core`` in
``road_trip_planning_team.pipeline`` — a neutral module, so the worker does not
import the FastAPI app) so the job-store bookkeeping (RUNNING → COMPLETED,
FAILED) lives in exactly one place. Status is written to the durable
``JobServiceClient`` store, so a completed run survives a worker/process
restart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from road_trip_planning_team.temporal.constants import TASK_QUEUE

PIPELINE_TIMEOUT = timedelta(hours=2)

# The road-trip pipeline is a long, non-idempotent LLM graph, and the
# llm_service layer already fails over on transient provider errors. A
# workflow-level retry would therefore mostly re-run expensive deterministic
# failures, so cap at a single attempt: a failure surfaces as a failed workflow
# plus a FAILED job-store row for explicit resubmission rather than being
# auto-retried.
NO_RETRY = RetryPolicy(maximum_attempts=1)


@activity.defn(name="road_trip_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the road-trip pipeline and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store
          (the API endpoint calls ``create_job`` before dispatch).
        - ``request`` is the serialized ``PlanTripRequest`` (``body.model_dump()``).

    Postconditions:
        - On success the job store row ends in COMPLETED with the itinerary
          result and the activity returns ``{"job_id": job_id}``.
        - On failure, marks the row FAILED and re-raises so the failure surfaces
          as a failed Temporal workflow rather than a silently-"completed" one.
          Auto-retry is bounded by ``NO_RETRY`` (see ``RoadTripWorkflow.run``).
    """
    from road_trip_planning_team.models import PlanTripRequest
    from road_trip_planning_team.pipeline import run_plan_core
    from road_trip_planning_team.shared.job_store import JOB_STATUS_FAILED, update_job

    body = PlanTripRequest(**request)
    try:
        run_plan_core(job_id, body)
    except Exception as e:
        activity.logger.exception("Road trip planning job %s failed", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
        raise
    return {"job_id": job_id}


@workflow.defn(name="RoadTripWorkflow")
class RoadTripWorkflow:
    """Runs one road-trip planning job as a single durable activity.

    Invariants:
        - Job-store status bookkeeping (RUNNING → COMPLETED/FAILED) is owned by
          the activity, not the workflow; the workflow only dispatches and
          propagates the activity's failure.
    """

    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the road-trip pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``PlanTripRequest`` (``body.model_dump()``).

        Postconditions:
            - Delegates to ``run_pipeline_activity`` (which owns job-store status
              bookkeeping) and returns its ``{"job_id": job_id}`` result.
        """
        return await workflow.execute_activity(
            run_pipeline_activity,
            args=[job_id, request],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=PIPELINE_TIMEOUT,
            retry_policy=NO_RETRY,
        )
