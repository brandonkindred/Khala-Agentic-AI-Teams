"""Start blogging Temporal workflows from sync API. Uses run_coroutine_threadsafe on the worker's event loop."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from blogging.temporal.client import get_temporal_client, get_temporal_loop
from blogging.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX_FULL_PIPELINE
from blogging.temporal.workflows import BlogFullPipelineWorkflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the blogging worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_full_pipeline_workflow(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Start BlogFullPipelineWorkflow for the given job and request."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX_FULL_PIPELINE}{job_id}"
    _run_async(
        client.start_workflow(
            BlogFullPipelineWorkflow.run,
            args=[job_id, request_dict],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started BlogFullPipelineWorkflow id=%s", workflow_id)
