"""Start Agent Provisioning Temporal workflows from sync API.

``start_provisioning_workflow`` is fire-and-forget (returns as soon as Temporal
accepts the start). ``run_deprovision_workflow`` is execute-and-wait because the
HTTP ``DELETE`` handler must return the deprovision payload in the response.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Coroutine, Optional, TypeVar

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

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

_T = TypeVar("_T")


def _start_workflow_timeout_s() -> float:
    """Parse ``AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S`` (default 30)."""
    raw = os.environ.get("AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S", "30")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 30.0
    return max(1.0, value)


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Block until ``coro`` completes on the shared Temporal event loop.

    Used for fire-and-forget ``client.start_workflow`` (not
    ``execute_workflow_sync``): we only need the start to be accepted, not the
    full workflow result. Deprovision uses execute-and-wait instead.
    """
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        coro.close()
        raise RuntimeError(
            "Temporal client not available; is TEMPORAL_ADDRESS set and the Temporal server reachable?"
        )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=_start_workflow_timeout_s())


def start_provisioning_workflow(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    skip_phases: Optional[list[str]] = None,
    prior_results: Optional[dict[str, Any]] = None,
    *,
    replace_existing: bool = False,
) -> None:
    """Start ``AgentProvisioningWorkflow`` for the given job (fire-and-forget).

    Preconditions:
        * ``job_id``, ``agent_id``, and ``manifest_path`` are non-empty.
        * The shared Temporal client/loop are available (``TEMPORAL_ADDRESS`` set
          and the worker process has connected).
        * When ``replace_existing`` is True (resume/restart after hard cutover),
          any open execution with the same stable workflow id is terminated so
          abandoned former-type runs do not block migration.
    Postconditions:
        * Temporal has accepted a workflow id ``{WORKFLOW_ID_PREFIX}{job_id}``.
        * ``skip_phases`` / ``prior_results`` are forwarded for ``/resume`` parity.
    Raises:
        * ``RuntimeError`` when the Temporal client/loop is unavailable.
        * ``concurrent.futures.TimeoutError`` when start acceptance exceeds
          ``AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S`` (the start coroutine may
          still complete afterward — callers must not treat this as a proven
          non-start).
    """
    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    assert manifest_path, "manifest_path must be non-empty"

    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_kwargs: dict[str, Any] = {
        "id": workflow_id,
        "task_queue": TASK_QUEUE,
    }
    if replace_existing:
        # Hard cutover leaves abandoned executions open under the same stable id.
        start_kwargs["id_reuse_policy"] = WorkflowIDReusePolicy.TERMINATE_IF_RUNNING
        start_kwargs["id_conflict_policy"] = WorkflowIDConflictPolicy.TERMINATE_EXISTING

    async def _start() -> None:
        client = get_temporal_client()
        # ``_run_async`` already guards for None client/loop; this narrows the type.
        assert client is not None
        await client.start_workflow(
            AgentProvisioningWorkflow.run,
            args=[
                job_id,
                agent_id,
                manifest_path,
                list(skip_phases) if skip_phases else None,
                dict(prior_results) if prior_results else None,
            ],
            **start_kwargs,
        )

    _run_async(_start())
    logger.info(
        "Started AgentProvisioningWorkflow id=%s replace_existing=%s",
        workflow_id,
        replace_existing,
    )


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
