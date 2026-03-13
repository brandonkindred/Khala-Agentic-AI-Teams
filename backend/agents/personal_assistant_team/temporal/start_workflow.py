"""Start PA Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from personal_assistant_team.temporal.client import get_temporal_client, get_temporal_loop, is_temporal_enabled
from personal_assistant_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX_ASSISTANT
from personal_assistant_team.temporal.workflows import PaAssistantWorkflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the PA worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_assistant_workflow(
    job_id: str,
    user_id: str,
    message: str,
    context: Dict[str, Any],
) -> None:
    """Start PaAssistantWorkflow for the given job."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX_ASSISTANT}{job_id}"
    _run_async(
        client.start_workflow(
            PaAssistantWorkflow.run,
            args=[job_id, user_id, message, context or {}],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started PaAssistantWorkflow id=%s", workflow_id)
