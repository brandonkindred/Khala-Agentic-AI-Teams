"""Temporal workflows for the social media marketing team."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from social_media_marketing_team.temporal import activities as _activities
    from social_media_marketing_team.temporal.constants import TASK_QUEUE

RUN_TIMEOUT = timedelta(hours=4)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="SocialMarketingTeamWorkflow")
class SocialMarketingTeamWorkflow:
    """Runs one social marketing team job (run or revise) as an activity."""

    @workflow.run
    async def run(self, job_id: str, request_dict: Dict[str, Any]) -> None:
        await workflow.execute_activity(
            _activities.run_team_job_activity,
            args=[job_id, request_dict],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=RUN_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
