"""Temporal workflows for the Planning V3 team."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from planning_v3_team.temporal import activities as _activities
    from planning_v3_team.temporal.constants import TASK_QUEUE

WORKFLOW_TIMEOUT = timedelta(hours=12)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="PlanningV3Workflow")
class PlanningV3Workflow:
    """Runs one Planning V3 job as an activity."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        repo_path: str,
        client_name: Optional[str],
        initial_brief: Optional[str],
        spec_content: Optional[str],
        use_product_analysis: bool,
        use_planning_v2: bool,
        use_market_research: bool,
    ) -> None:
        await workflow.execute_activity(
            _activities.run_planning_v3_activity,
            args=[
                job_id,
                repo_path,
                client_name,
                initial_brief,
                spec_content,
                use_product_analysis,
                use_planning_v2,
                use_market_research,
            ],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=WORKFLOW_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
