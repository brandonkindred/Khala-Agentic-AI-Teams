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
(3600s). On timeout the caller gets ``RuntimeError`` (HTTP 500). Work runs on
daemon threads so interpreter exit does not join a 3600s LLM HTTP call.
``shutdown_authoring_executor`` is part of unified-API lifespan teardown: it
rejects new submits and queued slot waiters; an in-flight HTTP request cannot
be aborted from another thread.
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

_authoring_pool: _DaemonAuthoringPool | None = None
_authoring_pool_lock = threading.Lock()

T = TypeVar("T")


class _DaemonAuthoringPool:
    """Bounded authoring pool whose workers do not atexit-join the interpreter.

    CPython ``ThreadPoolExecutor`` registers ``_python_exit``, which joins
    running workers even when they are daemon threads. A stalled LLM call
    using ``resolve_timeout()`` (3600s) would then block reload/shutdown for
    up to an hour after the caller already timed out. Daemon ``Thread``s
    without that atexit hook let the process exit; in-flight HTTP still
    cannot be cancelled from another thread.
    """

    def __init__(self) -> None:
        self._sem = threading.BoundedSemaphore(_AUTHORING_POOL_WORKERS)
        self._lock = threading.Lock()
        self._shutdown = False

    def submit(self, fn: Callable[..., T], *args: object) -> concurrent.futures.Future[T]:
        """Run ``fn(*args)`` on a daemon thread, or fail if this pool is shut down.

        Preconditions:
            - ``fn`` is callable; ``args`` match ``fn``.
        Postconditions:
            - Returns a future for the call, or a completed future whose
              exception is ``RuntimeError`` when the pool is shut down.
        """
        fut: concurrent.futures.Future[T] = concurrent.futures.Future()
        with self._lock:
            if self._shutdown:
                fut.set_exception(RuntimeError("Agent Studio authoring executor is shut down"))
                return fut

        def _worker() -> None:
            acquired = self._sem.acquire(timeout=AUTHORING_TIMEOUT_S)
            if not acquired:
                if not fut.done():
                    fut.set_exception(
                        RuntimeError(
                            "Agent Studio authoring call did not complete within the dispatch timeout"
                        )
                    )
                return
            try:
                with self._lock:
                    stopped = self._shutdown
                if stopped or fut.done():
                    if not fut.done():
                        fut.set_exception(RuntimeError("Agent Studio authoring executor is shut down"))
                    return
                try:
                    result = fn(*args)
                except Exception as exc:
                    if not fut.done():
                        fut.set_exception(exc)
                    return
                if not fut.done():
                    fut.set_result(result)
            finally:
                self._sem.release()

        threading.Thread(target=_worker, name="studio-authoring", daemon=True).start()
        return fut

    def shutdown(self) -> None:
        """Reject new work. In-flight daemon workers are not joined.

        Preconditions:
            - None.
        Postconditions:
            - Subsequent ``submit`` calls complete with ``RuntimeError``.
            - Idempotent.
        """
        with self._lock:
            self._shutdown = True

    def is_live(self) -> bool:
        """Return whether ``submit`` still accepts work.

        Preconditions:
            - None.
        Postconditions:
            - ``True`` until ``shutdown``; ``False`` after.
        """
        with self._lock:
            return not self._shutdown


def _authoring_executor() -> _DaemonAuthoringPool:
    """Return the process-wide pool that runs authoring CRUD off the FastAPI worker.

    Preconditions:
        - None.
    Postconditions:
        - Returns a live pool, recreating one after ``shutdown_authoring_executor``.
    """
    global _authoring_pool
    with _authoring_pool_lock:
        pool = _authoring_pool
        if pool is not None and pool.is_live():
            return pool
        _authoring_pool = _DaemonAuthoringPool()
        return _authoring_pool


def shutdown_authoring_executor() -> None:
    """Drop the authoring pool so unified-API lifespan can tear down without joining LLM HTTP.

    Preconditions:
        - None.
    Postconditions:
        - The current pool rejects new submits; the module slot is cleared so
          the next CRUD call (reload / in-process test app) gets a fresh pool.
        - Idempotent when no pool exists.
    """
    global _authoring_pool
    with _authoring_pool_lock:
        pool = _authoring_pool
        _authoring_pool = None
    if pool is not None:
        pool.shutdown()


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
