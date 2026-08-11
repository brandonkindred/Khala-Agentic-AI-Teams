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
        STANDALONE_TYPE_PRODUCT_ANALYSIS,
        TASK_QUEUE,
    )

RETRY_FAILED_TIMEOUT = timedelta(seconds=24 * 3600)
STANDALONE_TIMEOUT = timedelta(seconds=12 * 3600)


# Retry policy: limited retries for transient failures
DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="RetryFailedWorkflow")
class RetryFailedWorkflow:
    """Runs retry_failed (run_failed_tasks) for a job."""

    @workflow.run
    async def run(self, job_id: str) -> None:
        trace_id = workflow.uuid4().hex[:12]
        await workflow.execute_activity(
            _activities.retry_failed_activity,
            args=[job_id, trace_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=RETRY_FAILED_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )


@workflow.defn(name="RunTeamWorkflowV2")
class RunTeamWorkflowV2:
    """Multi-step orchestration: each pipeline phase is a separate Temporal activity.

    Phases: spec parsing + PRA → Planning → Coding Team execution.
    Each activity can fail and retry independently.
    """

    @workflow.run
    async def run(
        self,
        job_id: str,
        repo_path: str,
        spec_content_override: Optional[str] = None,
        resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
        planning_only: bool = False,
        sprint_id: Optional[str] = None,
    ) -> None:
        # One trace id for every phase of this job — generated via workflow.uuid4()
        # (Temporal's replay-safe UUID source) rather than
        # shared.observability.new_trace_id()/uuid.uuid4() directly, since workflow
        # code must be deterministic across replays. Each activity runs as its own
        # process/thread invocation, so the id is passed explicitly and re-bound
        # inside each activity rather than relying on contextvar inheritance.
        # ``.hex[:12]`` mirrors new_trace_id()'s documented shape, so both runtime
        # modes emit the same id format.
        trace_id = workflow.uuid4().hex[:12]

        # Phase 1: Spec parsing + Product Requirements Analysis
        spec_result = await workflow.execute_activity(
            _activities.parse_spec_activity,
            args=[job_id, repo_path, spec_content_override, trace_id, sprint_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=timedelta(hours=4),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Phase 2: Planning
        plan_result = await workflow.execute_activity(
            _activities.plan_project_activity,
            args=[job_id, repo_path, spec_result, trace_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=timedelta(hours=4),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        if planning_only:
            return

        # Phase 3: Coding Team execution
        await workflow.execute_activity(
            _activities.execute_coding_team_activity,
            args=[job_id, repo_path, plan_result, resolved_questions_override, trace_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=timedelta(hours=36),
            heartbeat_timeout=timedelta(minutes=10),
            retry_policy=DEFAULT_RETRY_POLICY,
        )


@workflow.defn(name="StandaloneJobWorkflow")
class StandaloneJobWorkflow:
    """Runs a standalone job (frontend-code-v2, backend-code-v2, product-analysis)."""

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
