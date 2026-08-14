"""In-process dispatch for the Agent Studio authoring CRUD operations.

The synchronous route handlers call these helpers. Each helper delegates to the
matching :class:`~agent_platform.studio.service.AgentStudioService` method on the
process-wide singleton (:func:`agent_platform.studio.runtime.get_studio_service`).
Authoring CRUD (start conversation, send message, clone, save) does **not** start
Temporal workflows: the former 1-activity wrappers are gone, so a configured
Temporal cluster or an in-process ``agent_studio`` worker is never required for
these paths. Native ``ValueError`` / ``LookupError`` from the service propagate
unchanged; the route maps them to 400 / 404.

Each helper waits at most :data:`AUTHORING_TIMEOUT_S` (180s, matching the former
Temporal activity ``start_to_close_timeout``). That bound keeps a stalled LLM
provider from occupying a FastAPI threadpool worker until the client default
(3600s). On timeout the caller gets ``RuntimeError`` (HTTP 500); the service
call may still finish on the dedicated authoring pool.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from agent_platform.studio.service import AgentStudioService

from agent_platform.registry.models import AgentManifest
from agent_platform.studio.models import AgentDefinition, ConversationStateResponse

# Former Temporal activity cap for one authoring op. Tighter than
# ``execute_workflow_sync``'s 300s HTTP wait; well below ``resolve_timeout()``'s
# 3600s LLM-client default.
AUTHORING_TIMEOUT_S = 180.0
_AUTHORING_POOL_WORKERS = 4

_authoring_pool: concurrent.futures.ThreadPoolExecutor | None = None
_authoring_pool_lock = threading.Lock()

T = TypeVar("T")


def _authoring_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the process-wide pool that runs authoring CRUD off the FastAPI worker.

    Preconditions:
        - None.
    Postconditions:
        - Returns the same executor on every call within a process.
    """
    global _authoring_pool
    with _authoring_pool_lock:
        if _authoring_pool is None:
            _authoring_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_AUTHORING_POOL_WORKERS,
                thread_name_prefix="studio-authoring",
            )
        return _authoring_pool


def _run(fn: Callable[..., T], *args: object) -> T:
    """Run ``fn`` with a bounded wait so a stalled provider cannot pin the caller.

    Preconditions:
        - ``fn`` is callable; ``args`` match ``fn``.
    Postconditions:
        - Returns ``fn``'s result, or re-raises ``fn``'s exception.
        - Raises ``RuntimeError`` if ``fn`` does not finish within
          ``AUTHORING_TIMEOUT_S``.
    """
    fut = _authoring_executor().submit(fn, *args)
    try:
        return fut.result(timeout=AUTHORING_TIMEOUT_S)
    except concurrent.futures.TimeoutError as exc:
        raise RuntimeError(
            "Agent Studio authoring call did not complete within the dispatch timeout"
        ) from exc


def _direct_service() -> "AgentStudioService":
    """Return the process-wide service singleton.

    Imported lazily so tests can monkeypatch
    ``agent_platform.studio.runtime.get_studio_service`` and have this path pick
    up the stand-in, rather than binding a stale reference at import time.

    Preconditions:
        - None.
    Postconditions:
        - Returns the process-wide ``AgentStudioService`` singleton.
    """
    from agent_platform.studio.runtime import get_studio_service

    return get_studio_service()


def start_conversation(
    mode: str, source_agent_id: str | None, initial_message: str | None
) -> ConversationStateResponse:
    """Start an authoring conversation in-process.

    Preconditions:
        - Arguments match ``AgentStudioService.start_conversation``.
    Postconditions:
        - Returns the initial ``ConversationStateResponse``; raises the service's
          native ``ValueError``/``LookupError`` on a bad request / unknown source.
        - Raises ``RuntimeError`` if the call exceeds ``AUTHORING_TIMEOUT_S``.
    """
    return _run(_direct_service().start_conversation, mode, source_agent_id, initial_message)


def send_message(conversation_id: str, message: str) -> ConversationStateResponse:
    """Send a message in-process.

    Preconditions:
        - Arguments match ``AgentStudioService.send_message``.
    Postconditions:
        - Returns the updated ``ConversationStateResponse``; raises native
          ``ValueError``/``LookupError`` on invalid input / unknown conversation.
        - Raises ``RuntimeError`` if the call exceeds ``AUTHORING_TIMEOUT_S``.
    """
    return _run(_direct_service().send_message, conversation_id, message)


def clone_from_registry(agent_id: str) -> AgentDefinition:
    """Clone a registered agent into a refine-mode draft in-process.

    Preconditions:
        - ``agent_id`` is the registry id to clone.
    Postconditions:
        - Returns the cloned ``AgentDefinition``; raises native ``LookupError``
          when ``agent_id`` names no registered agent.
        - Raises ``RuntimeError`` if the call exceeds ``AUTHORING_TIMEOUT_S``.
    """
    return _run(_direct_service().clone_from_registry, agent_id)


def save_agent(definition: AgentDefinition) -> tuple[AgentManifest, bool]:
    """Save + register a definition in-process.

    Mirrors ``AgentStudioService.save_agent``'s return shape so the route stays
    structurally identical.

    Preconditions:
        - ``definition`` is an ``AgentDefinition``.
    Postconditions:
        - Returns ``(AgentManifest, created)``; raises native ``ValueError`` when
          the definition is not ready to save.
        - Raises ``RuntimeError`` if the call exceeds ``AUTHORING_TIMEOUT_S``.
    """
    return _run(_direct_service().save_agent, definition)
