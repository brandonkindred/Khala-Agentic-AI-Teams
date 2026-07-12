"""Dispatch helpers that route sandbox lifecycle ops to Temporal.

The unified-API sandbox routes / runner / reaper-startup call these instead of
``agent_provisioning_team.sandbox`` directly when Temporal is enabled. Keeping
the branch here (rather than inside the sandbox package) preserves layering: the
sandbox package stays unaware of Temporal, and these helpers depend on both.

``acquire``/``teardown`` are async (awaited from ``async def`` routes) and use
the non-blocking :func:`shared_temporal.runner.execute_workflow_async` bridge so
the API event loop is never blocked for the duration of a ~90s cold start. The
reaper is started once at boot via the blocking sync bridge (acceptable during
lifespan startup; callers may wrap it in ``asyncio.to_thread``).
"""

from __future__ import annotations

import logging
import uuid

from temporalio.exceptions import WorkflowAlreadyStartedError

from agent_provisioning_team.sandbox.state import SandboxHandle
from agent_provisioning_team.temporal.constants import (
    SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S,
    SANDBOX_REAPER_INTERVAL_S,
    SANDBOX_REAPER_WORKFLOW_ID,
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
)
from agent_provisioning_team.temporal.sandbox_workflows import (
    SandboxAcquireWorkflow,
    SandboxReaperWorkflow,
    SandboxTeardownWorkflow,
)
from shared_temporal.runner import execute_workflow_async, start_workflow_sync

logger = logging.getLogger(__name__)


def sandbox_temporal_enabled() -> bool:
    """True when sandbox lifecycle ops should dispatch to Temporal.

    Gated on ``TEMPORAL_ADDRESS`` being set and the ``PROVISION_THREAD_FALLBACK``
    escape hatch being off, so one switch forces the whole Agent Provisioning
    team (provisioning + sandbox) back to in-process execution. The escape-hatch
    check itself lives in :func:`agent_provisioning_team.temporal.client.provision_thread_fallback_enabled`
    — the single source of truth shared with ``api/main.py``'s provisioning and
    deprovision dispatch, so this can never independently drift.

    Postconditions:
        * Returns ``False`` (never raises) when Temporal is unavailable, so the
          caller falls back to the direct in-process path.
    """
    from agent_provisioning_team.temporal.client import (
        is_temporal_enabled,
        provision_thread_fallback_enabled,
    )

    if provision_thread_fallback_enabled():
        return False
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
    if sandbox_temporal_enabled():
        return await acquire_sandbox_via_temporal(agent_id)
    from agent_provisioning_team.sandbox import acquire as _acquire

    return await _acquire(agent_id)


async def teardown_sandbox(agent_id: str) -> None:
    """Tear down ``agent_id``'s sandbox, via Temporal when enabled else in-process.

    Preconditions:
        * ``agent_id`` is non-empty; called from a running event loop.
    Postconditions:
        * The sandbox is torn down in whichever mode is active.
    """
    if sandbox_temporal_enabled():
        await teardown_sandbox_via_temporal(agent_id)
        return
    from agent_provisioning_team.sandbox import teardown as _teardown

    await _teardown(agent_id)


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
            task_queue=TASK_QUEUE,
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
    ``UnknownAgentError`` → 404 and ``DockerUnavailableError`` → 503, and
    ``reap_once()`` catches ``DockerError`` specifically around its own direct
    (non-Temporal) ``teardown()`` call. Temporal wraps the activity's
    exception, so we unwrap the ``ApplicationError`` and, on a recognized
    ``type`` name, re-raise the original exception class. Anything else
    (including a client-side ``asyncio.TimeoutError``, which carries no
    ``.type``) is left for the caller to re-raise unchanged.
    """
    # Walk the __cause__ chain to the ApplicationError carrying the type name.
    app_type: str | None = None
    message = str(exc)
    cursor: BaseException | None = exc
    seen = 0
    while cursor is not None and seen < 5:
        t = getattr(cursor, "type", None)
        if isinstance(t, str):
            app_type = t
            message = getattr(cursor, "message", None) or message
            break
        cursor = getattr(cursor, "cause", None) or cursor.__cause__
        seen += 1
    if app_type is None:
        return
    from agent_provisioning_team.sandbox import DockerUnavailableError, UnknownAgentError
    from agent_provisioning_team.sandbox.provisioner import DockerError

    if app_type == "UnknownAgentError":
        raise UnknownAgentError(message) from exc
    if app_type == "DockerUnavailableError":
        raise DockerUnavailableError(message) from exc
    if app_type == "DockerError":
        raise DockerError(message) from exc


async def teardown_sandbox_via_temporal(agent_id: str) -> None:
    """Tear down ``agent_id``'s sandbox via ``SandboxTeardownWorkflow``.

    Preconditions:
        * ``agent_id`` is non-empty; called from a running event loop.
    Postconditions:
        * The sandbox is torn down. On failure, raises the same exception type
          the in-process ``teardown()`` path would have raised (e.g.
          ``DockerError``) — see :func:`_reraise_sandbox_error` — rather than
          an opaque ``WorkflowFailureError``, for parity with
          :func:`acquire_sandbox_via_temporal`.
    """
    assert agent_id, "agent_id must be non-empty"
    workflow_id = f"{WORKFLOW_ID_PREFIX}sandbox-teardown-{agent_id}-{uuid.uuid4().hex[:8]}"
    try:
        await execute_workflow_async(
            SandboxTeardownWorkflow.run,
            agent_id,
            workflow_id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001
        _reraise_sandbox_error(exc)
        raise


def start_sandbox_reaper_workflow(interval_s: int = SANDBOX_REAPER_INTERVAL_S) -> None:
    """Start the singleton idle-reaper workflow (fixed id); no-op if already running.

    Preconditions:
        * The Agent Provisioning Temporal worker is running (Temporal enabled).
    Postconditions:
        * Exactly one ``SandboxReaperWorkflow`` runs on the shared task queue. A
          ``WorkflowAlreadyStartedError`` (a reaper already running, e.g. after a
          restart or from a sibling replica) is swallowed — that IS the desired
          single-instance behavior. Any other exception propagates.
    """
    try:
        start_workflow_sync(
            SandboxReaperWorkflow.run,
            interval_s,
            workflow_id=SANDBOX_REAPER_WORKFLOW_ID,
            task_queue=TASK_QUEUE,
        )
        logger.info("Started SandboxReaperWorkflow id=%s", SANDBOX_REAPER_WORKFLOW_ID)
    except WorkflowAlreadyStartedError:
        logger.info("SandboxReaperWorkflow already running; not starting another")
