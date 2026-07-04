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

# maximum_attempts=1: _run_branding_background already captures pipeline errors
# into a FAILED job row, so an app-level retry would re-run an
# already-finalized job. Durability against a hard process/worker restart comes
# from Temporal re-delivering the not-yet-completed activity, not from retries.
NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn(name="BrandingWorkflow")
class BrandingWorkflow:
    """Runs one branding job as a single durable activity."""

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> None:
        await workflow.execute_activity(
            _activities.run_branding_pipeline_activity,
            payload,
            task_queue=TASK_QUEUE,
            start_to_close_timeout=PIPELINE_TIMEOUT,
            retry_policy=NO_RETRY,
        )
