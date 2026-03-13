"""Start Agent Provisioning Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_provisioning_team.temporal.client import get_temporal_client, get_temporal_loop, is_temporal_enabled
from agent_provisioning_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from agent_provisioning_team.temporal.workflows import AgentProvisioningWorkflow

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30


def _run_async(coro: Any) -> Any:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError("Temporal client not available; is the Agent Provisioning worker running?")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_provisioning_workflow(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier_str: str,
) -> None:
    """Start AgentProvisioningWorkflow for the given job."""
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(
            AgentProvisioningWorkflow.run,
            args=[job_id, agent_id, manifest_path, access_tier_str],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started AgentProvisioningWorkflow id=%s", workflow_id)
