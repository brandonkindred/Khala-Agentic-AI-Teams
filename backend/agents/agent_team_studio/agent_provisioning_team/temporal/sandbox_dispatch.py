"""Dispatch helpers that route sandbox lifecycle ops to Temporal.

The unified-API sandbox routes / runner / reaper-startup call these instead of
``agent_team_studio.agent_provisioning_team.sandbox`` directly when Temporal is enabled. Keeping
the branch here (rather than inside the sandbox package) preserves layering: the
sandbox package stays unaware of Temporal, and these helpers depend on both.

``acquire``/``teardown`` are async (awaited from ``async def`` routes) and use
the non-blocking :func:`shared.temporal.runner.execute_workflow_async` bridge so
the API event loop is never blocked for the duration of a ~90s cold start. The
reaper is started once at boot via the blocking sync bridge (acceptable during
lifespan startup; callers may wrap it in ``asyncio.to_thread``).
"""

from __future__ import annotations

import logging
import uuid

from temporalio.exceptions import WorkflowAlreadyStartedError

from agent_team_studio.agent_provisioning_team.sandbox import (
    DockerUnavailableError,
    SandboxAcquireFailedError,
    UnknownAgentError,
)
from agent_team_studio.agent_provisioning_team.sandbox import (
    acquire as _acquire_sandbox_inprocess,
)
from agent_team_studio.agent_provisioning_team.sandbox import (
    teardown as _teardown_sandbox_inprocess,
)
from agent_team_studio.agent_provisioning_team.sandbox.provisioner import DockerError
from agent_team_studio.agent_provisioning_team.sandbox.state import SandboxHandle
from agent_team_studio.agent_provisioning_team.temporal.constants import (
    SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S,
    SANDBOX_REAPER_INTERVAL_S,
    SANDBOX_REAPER_WORKFLOW_ID,
    SANDBOX_TASK_QUEUE,
    SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S,
    WORKFLOW_ID_PREFIX,
)
from agent_team_studio.agent_provisioning_team.temporal.sandbox_workflows import (
    SandboxAcquireWorkflow,
    SandboxReaperWorkflow,
    SandboxTeardownWorkflow,
)
from shared.temporal import translate_workflow_failure
from shared.temporal.runner import execute_workflow_async, start_workflow_sync

logger = logging.getLogger(__name__)


def sandbox_temporal_enabled() -> bool:
    """True when sandbox lifecycle ops should dispatch to Temporal.

    Postconditions:
        * Returns ``is_temporal_enabled()`` — never raises.
    """
    from shared.temporal.client import is_temporal_enabled

    return is_temporal_enabled()


async def acquire_sandbox(agent_id: str) -> SandboxHandle:
    """Warm ``agent_id``'s sandbox, via Temporal when enabled else in-process.

    The single entry point the routes/runner call. Read-only sandbox ops
    (``status``/``list``/``metrics``/``note_activity``) intentionally stay direct
    calls; only the lock-taking mutators route through Temporal (Invariant A).

    Preconditions:
        * ``agent_id`` is non-empty; called from a running event loop.
    Postconditions:
        * Returns the resulting :class:`SandboxHandle`. Raises the same
          ``UnknownAgentError`` / ``DockerUnavailableError`` types in both modes.
    """
    assert agent_id, "agent_id must be non-empty"
    if sandbox_temporal_enabled():
        return await acquire_sandbox_via_temporal(agent_id)
    return await _acquire_sandbox_inprocess(agent_id)


async def teardown_sandbox(agent_id: str) -> None:
    """Tear down ``agent_id``'s sandbox, via Temporal when enabled else in-process.

    Preconditions:
        * ``agent_id`` is non-empty; called from a running event loop.
    Postconditions:
        * The sandbox is torn down in whichever mode is active.
    """
    assert agent_id, "agent_id must be non-empty"
    if sandbox_temporal_enabled():
        await teardown_sandbox_via_temporal(agent_id)
        return
    await _teardown_sandbox_inprocess(agent_id)


async def acquire_sandbox_via_temporal(agent_id: str) -> SandboxHandle:
    """Warm ``agent_id``'s sandbox via ``SandboxAcquireWorkflow`` (execute-and-wait).

    Preconditions:
        * ``agent_id`` is non-empty; called from a running event loop.
    Postconditions:
        * Returns the resulting :class:`SandboxHandle`. A fresh workflow id is
          minted per call so repeated warms of the same agent never collide.
          The client wait (``SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S``) exceeds the
          workflow's own worst-case retry budget, so a legitimately-retrying
          acquire is not mistaken for a hung one.
    """
    assert agent_id, "agent_id must be non-empty"
    workflow_id = f"{WORKFLOW_ID_PREFIX}sandbox-acquire-{agent_id}-{uuid.uuid4().hex[:8]}"
    try:
        dump = await execute_workflow_async(
            SandboxAcquireWorkflow.run,
            agent_id,
            workflow_id=workflow_id,
            task_queue=SANDBOX_TASK_QUEUE,
            execute_timeout_s=SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        _reraise_sandbox_error(exc)
        raise
    return SandboxHandle.model_validate(dump)


def _reraise_sandbox_error(exc: Exception) -> None:
    """Translate a Temporal ``WorkflowFailureError`` back to the sandbox
    exception the callers already map to HTTP status codes.

    Preserves parity with the in-process path: the ``/warm`` route maps
    ``UnknownAgentError`` → 404, ``DockerUnavailableError`` → 503, and
    ``SandboxAcquireFailedError`` → 503; ``reap_once()`` catches ``DockerError``
    specifically around its own direct (non-Temporal) ``teardown()`` call.
    Delegates the actual cause-chain walk to
    :func:`shared.temporal.translate_workflow_failure` — the codebase's single
    shared implementation of "walk the chain, match an ``ApplicationError``
    type marker, re-raise the native exception" — rather than a bespoke walk.
    A client-side ``asyncio.TimeoutError`` (which carries no ``.type``) has no
    match and is left for the caller to re-raise unchanged.
    """
    translate_workflow_failure(
        exc,
        {
            "UnknownAgentError": UnknownAgentError,
            "DockerUnavailableError": DockerUnavailableError,
            "DockerError": DockerError,
            "SandboxAcquireFailedError": SandboxAcquireFailedError,
        },
    )


async def teardown_sandbox_via_temporal(agent_id: str) -> None:
    """Tear down ``agent_id``'s sandbox via ``SandboxTeardownWorkflow``.

    Preconditions:
        * ``agent_id`` is non-empty; called from a running event loop.
    Postconditions:
        * The sandbox is torn down. On failure, raises the same exception type
          the in-process ``teardown()`` path would have raised (e.g.
          ``DockerError``) — see :func:`_reraise_sandbox_error` — rather than
          an opaque ``WorkflowFailureError``, for parity with
          :func:`acquire_sandbox_via_temporal`. The client wait
          (``SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S``) exceeds the workflow's own
          worst-case retry budget, so a legitimately-retrying teardown is not
          mistaken for a hung one.
    """
    assert agent_id, "agent_id must be non-empty"
    workflow_id = f"{WORKFLOW_ID_PREFIX}sandbox-teardown-{agent_id}-{uuid.uuid4().hex[:8]}"
    try:
        await execute_workflow_async(
            SandboxTeardownWorkflow.run,
            agent_id,
            workflow_id=workflow_id,
            task_queue=SANDBOX_TASK_QUEUE,
            execute_timeout_s=SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        _reraise_sandbox_error(exc)
        raise


def start_sandbox_reaper_workflow(
    interval_s: int = SANDBOX_REAPER_INTERVAL_S,
    *,
    client_ready_timeout_s: float | None = None,
) -> None:
    """Start the singleton idle-reaper workflow (fixed id); no-op if already running.

    Preconditions:
        * The Agent Provisioning sandbox Temporal worker
          (:func:`agent_team_studio.agent_provisioning_team.temporal.worker.start_agent_provisioning_sandbox_temporal_worker_thread`)
          is running, polling ``SANDBOX_TASK_QUEUE`` inside this same process.
    Postconditions:
        * Exactly one ``SandboxReaperWorkflow`` runs on ``SANDBOX_TASK_QUEUE``. A
          ``WorkflowAlreadyStartedError`` (a reaper already running, e.g. after a
          restart or from a sibling replica) is swallowed — that IS the desired
          single-instance behavior. Any other exception propagates.

    ``client_ready_timeout_s`` is forwarded to :func:`shared.temporal.start_workflow_sync`
    (which forwards it to its own internal client-readiness poll). Boot-time
    callers that already retry this whole call with their own backoff (e.g.
    ``unified_api.main._start_sandbox_reaper_with_retry``) should pass a short
    value here — otherwise the default 10s internal poll
    (``shared.temporal.runner.CLIENT_READY_TIMEOUT_S``) stacks underneath the
    caller's own backoff, so a single outer "attempt" can block for up to 10s
    before the outer retry loop even gets a chance to apply its own delay.
    """
    try:
        start_workflow_sync(
            SandboxReaperWorkflow.run,
            interval_s,
            workflow_id=SANDBOX_REAPER_WORKFLOW_ID,
            task_queue=SANDBOX_TASK_QUEUE,
            client_ready_timeout_s=client_ready_timeout_s,
        )
        logger.info("Started SandboxReaperWorkflow id=%s", SANDBOX_REAPER_WORKFLOW_ID)
    except WorkflowAlreadyStartedError:
        logger.info("SandboxReaperWorkflow already running; not starting another")
