"""Start Agent Provisioning Temporal workflows from sync API.

``start_provisioning_workflow`` is fire-and-forget (returns as soon as Temporal
accepts the start). ``run_deprovision_workflow`` is execute-and-wait because the
HTTP ``DELETE`` handler must return the deprovision payload in the response.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import uuid
from typing import Any, Coroutine, TypeVar

from temporalio.common import WorkflowIDReusePolicy

from agent_team_studio.agent_provisioning_team.temporal.constants import (
    DEPROVISION_CLIENT_TIMEOUT_S,
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
)
from agent_team_studio.agent_provisioning_team.temporal.workflows import (
    AgentDeprovisioningWorkflow,
    AgentProvisioningWorkflow,
)
from shared.temporal import get_temporal_client, get_temporal_loop
from shared.temporal.runner import _await_client, execute_workflow_sync

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Bound how long the API waits for Temporal to accept a start (not full run).
DEFAULT_START_WORKFLOW_TIMEOUT_S = 30.0
# Describe-status probe for resume/restart; keep short so HTTP latency stays low.
DEFAULT_DESCRIBE_TIMEOUT_S = 5.0


def _start_workflow_timeout_s() -> float:
    """Parse ``AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S``.

    Postconditions:
        * Always returns a float ``>= 1.0``.
        * Unparseable / overflowing / non-finite environment values fall back
          to ``DEFAULT_START_WORKFLOW_TIMEOUT_S`` (no caller precondition).
    """
    raw = os.environ.get(
        "AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S",
        str(DEFAULT_START_WORKFLOW_TIMEOUT_S),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        logger.warning(
            "AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S is not a valid float (%r); "
            "using default %s",
            raw,
            DEFAULT_START_WORKFLOW_TIMEOUT_S,
        )
        return DEFAULT_START_WORKFLOW_TIMEOUT_S
    if math.isnan(value) or math.isinf(value):
        logger.warning(
            "AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S is non-finite (%r); using default %s",
            raw,
            DEFAULT_START_WORKFLOW_TIMEOUT_S,
        )
        return DEFAULT_START_WORKFLOW_TIMEOUT_S
    return max(1.0, value)


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Block until ``coro`` completes on the shared Temporal event loop.

    Used for fire-and-forget ``client.start_workflow`` (not
    ``execute_workflow_sync``): we only need the start to be accepted, not the
    full workflow result. Deprovision uses execute-and-wait instead.

    Preconditions:
        * ``coro`` is a Temporal client coroutine scheduled onto the worker loop.
    Postconditions:
        * Returns the coroutine result once Temporal accepts the call.
    Raises:
        * ``RuntimeError`` when the worker client/loop never becomes ready
          within the shared client-ready timeout (cold-start race).
        * ``concurrent.futures.TimeoutError`` when start acceptance exceeds
          ``AGENT_PROVISIONING_START_WORKFLOW_TIMEOUT_S``.
        * Propagates exceptions from ``coro`` itself (for example
          ``temporalio.exceptions.WorkflowAlreadyStartedError`` or
          ``temporalio.service.RPCError``) — ``future.result`` re-raises them
          as the original type, not wrapped in ``TimeoutError``.
    """
    # Wait briefly for the worker thread to populate client/loop — immediate
    # None is a cold-start race, not a terminal Temporal outage.
    _, loop = _await_client()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=_start_workflow_timeout_s())


def start_provisioning_workflow(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    skip_phases: list[str] | None = None,
    prior_results: dict[str, Any] | None = None,
    *,
    replace_existing: bool = False,
) -> None:
    """Start ``AgentProvisioningWorkflow`` for the given job (fire-and-forget).

    Preconditions:
        * ``job_id``, ``agent_id``, and ``manifest_path`` are non-empty.
        * The shared Temporal client/loop become available within the client-ready
          wait (``TEMPORAL_ADDRESS`` set and the worker process connecting).
        * When ``replace_existing`` is True (resume/restart after a closed prior
          run), callers have already rejected an open execution via
          ``provisioning_workflow_is_open``; this start uses
          ``ALLOW_DUPLICATE`` so a concurrent second start fails with
          ``WorkflowAlreadyStartedError`` instead of ``TERMINATE_IF_RUNNING``
          (which would kill the first run without compensation).
    Postconditions:
        * Temporal has accepted a workflow id ``{WORKFLOW_ID_PREFIX}{job_id}``.
        * ``skip_phases`` / ``prior_results`` are forwarded for ``/resume`` parity.
    Raises:
        * ``RuntimeError`` when the Temporal client/loop is unavailable.
        * ``temporalio.exceptions.WorkflowAlreadyStartedError`` when another
          open run already owns the stable workflow id (concurrent resume/restart).
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
        # Reuse the stable id after a closed prior run. Never TERMINATE_IF_RUNNING:
        # a concurrent resume/restart must not kill an in-flight sibling without
        # compensation — Temporal rejects the duplicate start instead.
        start_kwargs["id_reuse_policy"] = WorkflowIDReusePolicy.ALLOW_DUPLICATE

    async def _start() -> None:
        client = get_temporal_client()
        # ``_run_async`` / ``_await_client`` already guards for None; this narrows the type.
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


def provisioning_workflow_is_open(
    job_id: str,
    *,
    timeout_s: float = DEFAULT_DESCRIBE_TIMEOUT_S,
) -> bool:
    """Return True when the stable provisioning workflow for ``job_id`` is still open.

    Used by resume/restart so jobs left ``pending``/``running`` after an
    indeterminate start timeout can be recovered when Temporal never accepted
    (or already closed) the workflow.

    Preconditions:
        * ``job_id`` is non-empty.
    Postconditions:
        * Returns ``True`` for RUNNING / CONTINUED_AS_NEW, or when the Temporal
          client/loop is unavailable / describe is indeterminate (conservative:
          avoid colliding with a live start).
        * Returns ``False`` when the workflow is missing or in a terminal status.
    """
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    assert job_id, "job_id must be non-empty"
    loop = get_temporal_loop()
    client = get_temporal_client()
    if loop is None or client is None:
        return True

    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"

    async def _status() -> WorkflowExecutionStatus:
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        return desc.status

    try:
        status = asyncio.run_coroutine_threadsafe(_status(), loop).result(timeout=timeout_s)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        logger.warning(
            "Describe provisioning workflow id=%s failed (%s); treating as open",
            workflow_id,
            exc,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Describe provisioning workflow id=%s failed (%s); treating as open",
            workflow_id,
            exc,
        )
        return True

    return status in (
        WorkflowExecutionStatus.RUNNING,
        WorkflowExecutionStatus.CONTINUED_AS_NEW,
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
