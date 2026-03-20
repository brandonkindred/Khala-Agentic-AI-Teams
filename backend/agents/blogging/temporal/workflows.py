"""Temporal workflows for the blogging team. Workflows schedule activities; all I/O runs in activities."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from blogging.temporal import activities as _activities
    from blogging.temporal.constants import TASK_QUEUE

FULL_PIPELINE_TIMEOUT = timedelta(hours=12)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="BlogFullPipelineWorkflow")
class BlogFullPipelineWorkflow:
    """Runs the full blog pipeline (research, planning, draft, copy-edit) as an activity."""

    @workflow.run
    async def run(self, job_id: str, request_dict: Dict[str, Any]) -> None:
        await workflow.execute_activity(
            _activities.run_full_pipeline_activity,
            args=[job_id, request_dict],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=FULL_PIPELINE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
