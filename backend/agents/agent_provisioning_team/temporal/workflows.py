"""Temporal workflows for the Agent Provisioning team."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent_provisioning_team.temporal import activities as _activities
    from agent_provisioning_team.temporal.constants import TASK_QUEUE

PROVISIONING_TIMEOUT = timedelta(hours=4)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="AgentProvisioningWorkflow")
class AgentProvisioningWorkflow:
    """Runs one provisioning job as an activity."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        access_tier_str: str,
    ) -> None:
        await workflow.execute_activity(
            _activities.run_provisioning_activity,
            args=[job_id, agent_id, manifest_path, access_tier_str],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PROVISIONING_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
