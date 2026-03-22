"""Start Planning V3 Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from planning_v3_team.temporal.client import (
    get_temporal_client,
    get_temporal_loop,
)
from planning_v3_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from planning_v3_team.temporal.workflows import PlanningV3Workflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the Planning V3 worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_planning_v3_workflow(
    job_id: str,
    repo_path: str,
    client_name: Optional[str],
    initial_brief: Optional[str],
    spec_content: Optional[str],
    use_product_analysis: bool,
    use_planning_v2: bool,
    use_market_research: bool,
) -> None:
    """Start PlanningV3Workflow for the given job."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(
            PlanningV3Workflow.run,
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
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started PlanningV3Workflow id=%s", workflow_id)
