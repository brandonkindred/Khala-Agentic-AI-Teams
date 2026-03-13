"""Temporal workflows for the SOC2 compliance team."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from soc2_compliance_team.temporal import activities as _activities
    from soc2_compliance_team.temporal.constants import TASK_QUEUE

AUDIT_TIMEOUT = timedelta(hours=4)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="Soc2AuditWorkflow")
class Soc2AuditWorkflow:
    """Runs one SOC2 audit job as an activity."""

    @workflow.run
    async def run(self, job_id: str, repo_path: str) -> None:
        await workflow.execute_activity(
            _activities.run_audit_activity,
            args=[job_id, repo_path],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=AUDIT_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
