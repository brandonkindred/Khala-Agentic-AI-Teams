"""Temporal workflows for the AI systems team."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ai_systems_team.temporal import activities as _activities
    from ai_systems_team.temporal.constants import TASK_QUEUE

BUILD_TIMEOUT = timedelta(hours=12)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="AISystemsBuildWorkflow")
class AISystemsBuildWorkflow:
    """Runs one AI system build job as an activity."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        project_name: str,
        spec_path: str,
        constraints: Dict[str, Any],
        output_dir: Optional[str],
    ) -> None:
        await workflow.execute_activity(
            _activities.run_build_activity,
            args=[job_id, project_name, spec_path, constraints, output_dir],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=BUILD_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
