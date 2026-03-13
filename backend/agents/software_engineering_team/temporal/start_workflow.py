"""
Sync helpers to start Temporal workflows from the SE API (sync endpoints).

Uses run_coroutine_threadsafe to run client.start_workflow on the worker's event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from software_engineering_team.temporal.client import (
    get_temporal_client,
    get_temporal_loop,
    is_temporal_enabled,
)
from software_engineering_team.temporal.constants import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX_RETRY_FAILED,
    WORKFLOW_ID_PREFIX_RUN_TEAM,
    WORKFLOW_ID_PREFIX_STANDALONE,
)
from software_engineering_team.temporal.workflows import (
    RunTeamWorkflow,
    RetryFailedWorkflow,
    StandaloneJobWorkflow,
)

logger = logging.getLogger(__name__)

# Timeout for run_coroutine_threadsafe when starting a workflow (seconds)
START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    """Run a coroutine on the Temporal client's event loop from sync code."""
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_run_team_workflow(
    job_id: str,
    repo_path: str,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
) -> None:
    """Start RunTeamWorkflow. Idempotent for same workflow_id (new run after terminal)."""
    workflow_id = f"{WORKFLOW_ID_PREFIX_RUN_TEAM}{job_id}"
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    _run_async(
        client.start_workflow(
            RunTeamWorkflow.run,
            args=[
                job_id,
                repo_path,
                spec_content_override,
                resolved_questions_override,
                planning_only,
            ],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started RunTeamWorkflow id=%s", workflow_id)


def start_retry_failed_workflow(job_id: str) -> None:
    """Start RetryFailedWorkflow for the given job."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX_RETRY_FAILED}{job_id}"
    _run_async(
        client.start_workflow(
            RetryFailedWorkflow.run,
            args=[job_id],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started RetryFailedWorkflow id=%s", workflow_id)


def start_standalone_workflow(
    job_type: str,
    job_id: str,
    repo_path: str,
    *,
    task_dict: Optional[Dict[str, Any]] = None,
    architecture_overview: str = "",
    spec_content: Optional[str] = None,
    inspiration_content: Optional[str] = None,
    initial_spec_path: Optional[str] = None,
) -> None:
    """Start StandaloneJobWorkflow (frontend-code-v2, backend-code-v2, planning-v2, product-analysis)."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX_STANDALONE}{job_type}-{job_id}"
    _run_async(
        client.start_workflow(
            StandaloneJobWorkflow.run,
            args=[
                job_type,
                job_id,
                repo_path,
                task_dict,
                architecture_overview,
                spec_content,
                inspiration_content,
                initial_spec_path,
            ],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started StandaloneJobWorkflow id=%s type=%s", workflow_id, job_type)


def cancel_run_team_workflow(job_id: str) -> bool:
    """Request cancellation of the RunTeamWorkflow for this job. Returns True if a handle was found and cancelled."""
    client = get_temporal_client()
    if client is None:
        return False
    try:
        workflow_id = f"{WORKFLOW_ID_PREFIX_RUN_TEAM}{job_id}"
        handle = client.get_workflow_handle(workflow_id)
        _run_async(handle.cancel())
        logger.info("Cancelled workflow id=%s", workflow_id)
        return True
    except Exception as e:
        logger.debug("Cancel workflow id=%s: %s", f"{WORKFLOW_ID_PREFIX_RUN_TEAM}{job_id}", e)
        return False
