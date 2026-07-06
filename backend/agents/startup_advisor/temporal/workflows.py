"""Temporal workflow + activity for the Startup Advisor team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without executing
any non-deterministic top-level code (e.g. ``os.getenv``, worker bootstrap).

The activity reuses ``startup_advisor.pipeline.run_advisor_core`` (the same
RUNNING/COMPLETED job-store bookkeeping the thread dispatch path uses) so that
state-machine transition lives in exactly one place. ``pipeline`` is a neutral
module with no FastAPI dependency, so importing it here (to run the activity)
never pulls in ``api.main``'s ``app = create_team_app(...)`` side effect
(DB pool registration, middleware, route wiring) into the worker process.
Status is written to the durable ``JobServiceClient`` store, so a completed
run survives a worker/process restart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from startup_advisor.temporal.constants import TASK_QUEUE

PIPELINE_TIMEOUT = timedelta(minutes=30)

# run_advisor_core appends the user message to the conversation store as a
# side effect; a workflow-level retry would replay that append and duplicate
# the message. Cap at a single attempt: a failure surfaces as a failed
# workflow plus a FAILED job-store row for explicit resubmission.
NO_RETRY = RetryPolicy(maximum_attempts=1)


@activity.defn(name="startup_advisor_run_pipeline")
def run_pipeline_activity(job_id: str, message: str) -> dict[str, Any]:
    """Run the advisor pipeline and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store
          (the API endpoint calls ``create_job`` before dispatch).
        - ``message`` is the user's chat message.

    Postconditions:
        - On success the job store row ends in COMPLETED with the serialized
          ``ConversationStateResponse`` and the activity returns
          ``{"job_id": job_id}``.
        - On failure, marks the row FAILED and re-raises the *original*
          exception so the failure surfaces as a failed Temporal workflow
          rather than a silently "completed" one. If the ``update_job(FAILED)``
          write itself raises (e.g. job-store connectivity lost), that
          failure is logged separately and does not replace or mask the
          original exception that gets re-raised. Auto-retry is bounded by
          ``NO_RETRY``.
    """
    from startup_advisor.pipeline import run_advisor_core
    from startup_advisor.shared.job_store import JOB_STATUS_FAILED, update_job

    try:
        run_advisor_core(job_id, message)
    except Exception as e:
        activity.logger.exception("Startup advisor job %s failed", job_id)
        try:
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
        except Exception:
            activity.logger.exception(
                "Failed to mark startup advisor job %s as FAILED after pipeline failure", job_id
            )
        raise
    return {"job_id": job_id}


@workflow.defn(name="StartupAdvisorWorkflow")
class StartupAdvisorWorkflow:
    """Runs one startup-advisor message job as a single durable activity.

    Invariants:
        - Job-store status bookkeeping (RUNNING -> COMPLETED/FAILED) is owned
          by the activity, not the workflow; the workflow only dispatches and
          propagates the activity's failure.
    """

    @workflow.run
    async def run(self, job_id: str, message: str) -> dict[str, Any]:
        """Durable entrypoint: run the advisor pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``message`` is the user's chat message.

        Postconditions:
            - Delegates to ``run_pipeline_activity`` (which owns job-store
              status bookkeeping) and returns its ``{"job_id": job_id}``
              result.
        """
        return await workflow.execute_activity(
            run_pipeline_activity,
            args=[job_id, message],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=PIPELINE_TIMEOUT,
            retry_policy=NO_RETRY,
        )
