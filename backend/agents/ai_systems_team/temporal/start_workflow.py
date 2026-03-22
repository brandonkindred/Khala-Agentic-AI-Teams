"""Start AI Systems Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from ai_systems_team.temporal.client import (
    get_temporal_client,
    get_temporal_loop,
)
from ai_systems_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX_BUILD
from ai_systems_team.temporal.workflows import AISystemsBuildWorkflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the AI Systems worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_build_workflow(
    job_id: str,
    project_name: str,
    spec_path: str,
    constraints: Dict[str, Any],
    output_dir: Optional[str] = None,
) -> None:
    """Start AISystemsBuildWorkflow for the given job."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX_BUILD}{job_id}"
    _run_async(
        client.start_workflow(
            AISystemsBuildWorkflow.run,
            args=[job_id, project_name, spec_path, constraints, output_dir],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started AISystemsBuildWorkflow id=%s", workflow_id)
