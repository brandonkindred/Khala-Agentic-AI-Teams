"""Async Temporal activities for the Agent Provisioning sandbox lifecycle.

These are **async** activities on purpose. They run on the Temporal worker's
event loop (not the sync thread-pool executor used by the provisioning
activities), so the process-wide ``Lifecycle`` singleton's per-agent
``asyncio.Lock`` objects always bind to a single loop — the worker loop. Every
state-*mutating* sandbox operation (acquire, teardown, reap) is dispatched
through these activities; the read-only routes (``status``/``list``/``metrics``/
``note_activity``) stay direct calls on the API loop.

See ``sandbox/lifecycle.py`` for the two invariants this upholds:
- **Invariant A (loop affinity):** all ``asyncio.Lock`` takers on one loop.
- **Invariant B (thread safety):** all ``_state`` access serialized by a
  ``threading.Lock`` (added to ``Lifecycle``), because these activities run on a
  different OS thread than the API routes.

Heavy imports (``get_lifecycle`` and its transitive ``httpx``/docker deps) live
inside the function bodies to keep this module's import cheap and side-effect
free — it is imported by the workflow module under
``workflow.unsafe.imports_passed_through()``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="agent_provisioning_sandbox_acquire")
async def sandbox_acquire_activity(agent_id: str) -> Dict[str, Any]:
    """Idempotently bring the sandbox for ``agent_id`` to WARM.

    Preconditions:
        * ``agent_id`` is a non-empty string with a manifest in the registry.
        * Runs as an async activity on the worker loop (Invariant A).
    Postconditions:
        * Returns ``SandboxHandle.model_dump(mode="json")`` for a WARM sandbox.
          ``UnknownAgentError`` / ``DockerUnavailableError`` propagate so
          Temporal surfaces them (both are in ``non_retryable_error_types``).
          ``Lifecycle.acquire()`` itself never raises for a transient failure
          during provisioning — it catches internally and returns a
          non-raising ERROR-status handle, so that its own direct/thread-mode
          callers always get a handle back. This activity re-raises
          ``SandboxAcquireFailedError`` on that ERROR status specifically so
          ``SANDBOX_ACQUIRE_RETRY_POLICY`` actually retries those transient
          failures instead of the workflow silently "succeeding" with an
          ERROR result on the first attempt. Using a dedicated type (rather
          than a bare ``RuntimeError``) lets
          ``sandbox_dispatch._reraise_sandbox_error`` recognize it by name once
          retries are exhausted and map it to a clean HTTP 503, instead of an
          opaque ``WorkflowFailureError``.
    """
    from agent_team_studio.agent_provisioning_team.sandbox import (
        SandboxAcquireFailedError,
        get_lifecycle,
    )
    from agent_team_studio.agent_provisioning_team.sandbox.state import SandboxStatus

    assert agent_id, "agent_id must be non-empty"
    activity.heartbeat("sandbox_acquire")
    handle = await get_lifecycle().acquire(agent_id)
    if handle.status == SandboxStatus.ERROR:
        raise SandboxAcquireFailedError(handle.error or f"Sandbox acquire failed for {agent_id}")
    return handle.model_dump(mode="json")


@activity.defn(name="agent_provisioning_sandbox_teardown")
async def sandbox_teardown_activity(agent_id: str) -> None:
    """Stop the sandbox for ``agent_id`` and evict it from state.

    Preconditions:
        * ``agent_id`` is a non-empty string.
        * Runs as an async activity on the worker loop (Invariant A).
    Postconditions:
        * The container is stopped and the state row removed. A real Docker
          failure (``DockerError``) propagates so Temporal retries.
    """
    from agent_team_studio.agent_provisioning_team.sandbox import get_lifecycle

    assert agent_id, "agent_id must be non-empty"
    activity.heartbeat("sandbox_teardown")
    await get_lifecycle().teardown(agent_id)


@activity.defn(name="agent_provisioning_sandbox_reap")
async def sandbox_reap_activity() -> List[str]:
    """Tear down every sandbox idle longer than the configured threshold.

    The idle threshold is read from the environment
    (``AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES``) **inside the activity**, never
    inside the workflow, so the reaper workflow body stays deterministic.

    Preconditions:
        * Runs as an async activity on the worker loop (Invariant A).
    Postconditions:
        * Returns the list of torn-down ``agent_id``s (possibly empty).
    """
    from agent_team_studio.agent_provisioning_team.sandbox import get_lifecycle
    from agent_team_studio.agent_provisioning_team.sandbox.state import idle_teardown_seconds

    threshold = idle_teardown_seconds()
    activity.heartbeat("sandbox_reap")
    return await get_lifecycle().reap_once(threshold=threshold)
