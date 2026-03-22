"""Start social marketing Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from social_media_marketing_team.temporal.client import (
    get_temporal_client,
    get_temporal_loop,
)
from social_media_marketing_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX_RUN
from social_media_marketing_team.temporal.workflows import SocialMarketingTeamWorkflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: object) -> object:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the social marketing worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_team_job_workflow(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Start SocialMarketingTeamWorkflow for the given job (run or revise)."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX_RUN}{job_id}"
    _run_async(
        client.start_workflow(
            SocialMarketingTeamWorkflow.run,
            args=[job_id, request_dict],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started SocialMarketingTeamWorkflow id=%s", workflow_id)
