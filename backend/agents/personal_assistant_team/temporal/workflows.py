"""Temporal workflows for the personal assistant team."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from personal_assistant_team.temporal import activities as _activities
    from personal_assistant_team.temporal.constants import TASK_QUEUE

ASSISTANT_TIMEOUT = timedelta(hours=2)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="PaAssistantWorkflow")
class PaAssistantWorkflow:
    """Runs one assistant job as an activity."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        user_id: str,
        message: str,
        context: Dict[str, Any],
    ) -> None:
        await workflow.execute_activity(
            _activities.run_assistant_activity,
            args=[job_id, user_id, message, context],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=ASSISTANT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
