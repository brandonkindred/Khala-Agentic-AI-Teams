"""
Sync helpers to start Temporal workflows from the SE API (sync endpoints).

Uses run_coroutine_threadsafe to run client.start_workflow on the worker's event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from shared.temporal.client import (
    get_temporal_client,
    get_temporal_loop,
)
from software_engineering_team.temporal.constants import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX_RETRY_FAILED,
    WORKFLOW_ID_PREFIX_RUN_TEAM,
    WORKFLOW_ID_PREFIX_STANDALONE,
)
from software_engineering_team.temporal.workflows import (
    RetryFailedWorkflow,
    RunTeamWorkflow,
    RunTeamWorkflowV2,
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


def is_workflow_v2_enabled() -> bool:
    """Whether SE team starts select ``RunTeamWorkflowV2`` (the default) or fall back to
    ``RunTeamWorkflow`` (V1, kept only for draining in-flight/legacy jobs).

    Preconditions: none.
    Postconditions: returns False only when ``SE_WORKFLOW_V2`` is explicitly set to a
        recognized falsy value ("0"/"false"/"no", case-insensitive, surrounding
        whitespace ignored); returns True for unset, blank, or any other value.
    """
    import os

    raw = os.environ.get("SE_WORKFLOW_V2", "").strip().lower()
    return raw not in ("0", "false", "no")


def start_run_team_workflow(
    job_id: str,
    repo_path: str,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
    sprint_id: Optional[str] = None,
) -> None:
    """Start RunTeamWorkflow. Idempotent for same workflow_id.

    ``RunTeamWorkflowV2`` is the default; set ``SE_WORKFLOW_V2`` to a falsy value
    ("0"/"false"/"no") to select the legacy ``RunTeamWorkflow`` (V1) for draining
    in-flight/legacy jobs. ``sprint_id`` is forwarded on both paths — ``RunTeamWorkflow``
    and ``RunTeamWorkflowV2`` both accept it as their trailing positional arg.
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX_RUN_TEAM}{job_id}"
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")

    use_v2 = is_workflow_v2_enabled()
    workflow_cls = RunTeamWorkflowV2 if use_v2 else RunTeamWorkflow

    args: List[Any] = [
        job_id,
        repo_path,
        spec_content_override,
        resolved_questions_override,
        planning_only,
        sprint_id,
    ]

    _run_async(
        client.start_workflow(
            workflow_cls.run,
            args=args,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started %s id=%s", workflow_cls.__name__, workflow_id)


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
    """Start StandaloneJobWorkflow (frontend-code-v2, backend-code-v2, product-analysis)."""
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
