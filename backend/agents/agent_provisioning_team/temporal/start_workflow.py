"""Start Agent Provisioning Temporal workflows from sync API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Coroutine, Optional, TypeVar

from agent_provisioning_team.temporal.constants import (
    DEPROVISION_CLIENT_TIMEOUT_S,
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
)
from agent_provisioning_team.temporal.workflows import (
    AgentDeprovisioningWorkflow,
    AgentProvisioningWorkflow,
)
from shared_temporal import get_temporal_client, get_temporal_loop
from shared_temporal.runner import execute_workflow_sync

logger = logging.getLogger(__name__)

START_WORKFLOW_TIMEOUT = 30

_T = TypeVar("_T")


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        raise RuntimeError(
            "Temporal client not available; is the Agent Provisioning worker running?"
        )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=START_WORKFLOW_TIMEOUT)


def start_provisioning_workflow(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    skip_phases: Optional[list[str]] = None,
    prior_results: Optional[dict[str, Any]] = None,
) -> None:
    """Start ``AgentProvisioningWorkflow`` for the given job.

    ``skip_phases`` (phase ``.value`` strings) and ``prior_results`` (dict
    keyed by phase value with serialized phase output) are forwarded to
    the workflow so ``/resume`` keeps parity with the thread path.
    """
    client = get_temporal_client()
    if client is None:
        raise RuntimeError("Temporal client not available")
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    _run_async(
        client.start_workflow(
            AgentProvisioningWorkflow.run,
            args=[
                job_id,
                agent_id,
                manifest_path,
                list(skip_phases) if skip_phases else None,
                dict(prior_results) if prior_results else None,
            ],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    )
    logger.info("Started AgentProvisioningWorkflow id=%s", workflow_id)


def run_deprovision_workflow(agent_id: str, force: bool = False) -> dict[str, Any]:
    """Run ``AgentDeprovisioningWorkflow`` and block for its result.

    Execute-and-wait dispatch used by the synchronous ``DELETE
    /environments/{agent_id}`` handler, so the caller gets back the real
    ``DeprovisionResponse`` payload rather than a job id to poll.

    Preconditions:
        * ``agent_id`` is non-empty.
        * The Agent Provisioning Temporal worker is running (Temporal enabled).
    Postconditions:
        * Returns the ``DeprovisionResponse.model_dump()`` dict produced by
          ``deprovision_activity``. A fresh workflow id is minted per call
          (execute-and-wait does not reuse ids for idempotency), so repeated
          deprovisions of the same agent never collide.
    """
    assert agent_id, "agent_id must be non-empty"
    workflow_id = f"{WORKFLOW_ID_PREFIX}deprovision-{agent_id}-{uuid.uuid4().hex[:8]}"
    result = execute_workflow_sync(
        AgentDeprovisioningWorkflow.run,
        agent_id,
        force,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
        execute_timeout_s=DEPROVISION_CLIENT_TIMEOUT_S,
    )
    logger.info("Ran AgentDeprovisioningWorkflow id=%s", workflow_id)
    return result
