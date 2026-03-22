"""
Temporal workflows for the software engineering team.

Workflows are deterministic; they only schedule activities. All I/O and LLM
calls happen inside activities.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from software_engineering_team.temporal import activities as _activities
    from software_engineering_team.temporal.constants import (
        STANDALONE_TYPE_BACKEND,
        STANDALONE_TYPE_FRONTEND,
        STANDALONE_TYPE_PLANNING,
        STANDALONE_TYPE_PRODUCT_ANALYSIS,
        TASK_QUEUE,
    )

RUN_ORCHESTRATOR_TIMEOUT = timedelta(seconds=48 * 3600)
RETRY_FAILED_TIMEOUT = timedelta(seconds=24 * 3600)
STANDALONE_TIMEOUT = timedelta(seconds=12 * 3600)


# Retry policy: limited retries for transient failures
DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="RunTeamWorkflow")
class RunTeamWorkflow:
    """Runs the main Tech Lead orchestrator (run_orchestrator) as an activity."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        repo_path: str,
        spec_content_override: Optional[str] = None,
        resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
        planning_only: bool = False,
    ) -> None:
        await workflow.execute_activity(
            _activities.run_orchestrator_activity,
            args=[
                job_id,
                repo_path,
                spec_content_override,
                resolved_questions_override,
                planning_only,
            ],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=RUN_ORCHESTRATOR_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )


@workflow.defn(name="RetryFailedWorkflow")
class RetryFailedWorkflow:
    """Runs retry_failed (run_failed_tasks) for a job."""

    @workflow.run
    async def run(self, job_id: str) -> None:
        await workflow.execute_activity(
            _activities.retry_failed_activity,
            args=[job_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=RETRY_FAILED_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )


@workflow.defn(name="StandaloneJobWorkflow")
class StandaloneJobWorkflow:
    """Runs a standalone job (frontend-code-v2, backend-code-v2, planning-v2, product-analysis)."""

    @workflow.run
    async def run(
        self,
        job_type: str,
        job_id: str,
        repo_path: str,
        task_dict: Optional[Dict[str, Any]] = None,
        architecture_overview: str = "",
        spec_content: Optional[str] = None,
        inspiration_content: Optional[str] = None,
        initial_spec_path: Optional[str] = None,
    ) -> None:
        if job_type == STANDALONE_TYPE_FRONTEND and task_dict is not None:
            await workflow.execute_activity(
                _activities.run_frontend_code_v2_activity,
                args=[job_id, repo_path, task_dict, architecture_overview],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=STANDALONE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        elif job_type == STANDALONE_TYPE_BACKEND and task_dict is not None:
            await workflow.execute_activity(
                _activities.run_backend_code_v2_activity,
                args=[job_id, repo_path, task_dict, architecture_overview],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=STANDALONE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        elif job_type == STANDALONE_TYPE_PLANNING and spec_content is not None:
            await workflow.execute_activity(
                _activities.run_planning_v2_activity,
                args=[job_id, repo_path, spec_content, inspiration_content],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=STANDALONE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        elif job_type == STANDALONE_TYPE_PRODUCT_ANALYSIS and spec_content is not None:
            await workflow.execute_activity(
                _activities.run_product_analysis_activity,
                args=[job_id, repo_path, spec_content, initial_spec_path],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=STANDALONE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        else:
            raise ValueError(f"Unknown or invalid job_type for StandaloneJobWorkflow: {job_type!r}")
