"""Temporal workflow for the Branding team.

A single-activity workflow: it forwards the serialized job ``payload`` to
``run_branding_pipeline_activity``. Only a ``dict`` crosses the workflow
boundary, so there is no pydantic sandbox concern.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from branding_team.temporal import activities as _activities
    from branding_team.temporal.constants import TASK_QUEUE

PIPELINE_TIMEOUT = timedelta(hours=2)

# The branding orchestrator is a long, non-idempotent pipeline (LLM/sibling-team
# calls), and llm_service already fails over on transient provider errors. A
# workflow-level retry would therefore mostly re-run expensive deterministic
# failures, so cap at a single attempt: a failure surfaces as a failed workflow
# plus a FAILED job-store row for explicit resubmission.
#
# Trade-off: because the single attempt is consumed, a worker crash mid-activity
# is NOT auto-re-dispatched either. Such an orphaned RUNNING job is reconciled to
# ``interrupted`` by the team_service startup recovery (not resumed) so the
# expensive non-idempotent pipeline is deliberately not silently re-run.
NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn(name="BrandingWorkflow")
class BrandingWorkflow:
    """Runs one branding job as a single durable activity.

    Invariants:
        - Job-store status bookkeeping (RUNNING → COMPLETED/FAILED) is owned by
          the activity, not the workflow; the workflow only dispatches and
          propagates the activity's failure.
    """

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> None:
        """Durable entrypoint: run the branding pipeline for ``payload``.

        Preconditions:
            - ``payload`` is the serialized job dict built by
              ``_submit_brand_run`` (``job_id`` plus serialized
              mission/human_review/brand_checks/target_phase).
            - ``payload['job_id']`` refers to a job already created in the store.
        Postconditions:
            - Delegates to ``run_branding_pipeline_activity`` (which owns the
              job-store transitions). Returns ``None`` on success; a pipeline
              failure re-raised by the activity fails the workflow (no retry, by
              ``NO_RETRY``).
        """
        await workflow.execute_activity(
            _activities.run_branding_pipeline_activity,
            payload,
            task_queue=TASK_QUEUE,
            start_to_close_timeout=PIPELINE_TIMEOUT,
            retry_policy=NO_RETRY,
        )
