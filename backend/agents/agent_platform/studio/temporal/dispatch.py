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
(3600s). On timeout the caller gets ``RuntimeError`` (HTTP 500). Work runs on a
small fixed pool of daemon threads pulling from a queue — concurrency and total
thread count stay capped at ``_AUTHORING_POOL_WORKERS`` regardless of request
burst size — so interpreter exit does not join a 3600s LLM HTTP call and a burst
queues rather than spawning a thread per call. Each task runs inside the
submitting thread's ``contextvars.copy_context()`` snapshot so LLM attribution /
trace context survives the hand-off. ``shutdown_authoring_executor`` is part of
unified-API lifespan teardown: it rejects new submits and wakes the idle workers
to exit; an in-flight HTTP request cannot be aborted from another thread.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import queue
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

# Process-wide authoring pool, lazily instantiated by ``_authoring_executor``.
# ``_authoring_pool_lock`` guards every read/replace of this slot, so lazy
# construction and ``shutdown_authoring_executor`` are thread-safe.
_authoring_pool: _DaemonAuthoringPool | None = None
_authoring_pool_lock = threading.Lock()

T = TypeVar("T")


class _DaemonAuthoringPool:
    """Fixed-size daemon-worker pool whose workers do not atexit-join the interpreter.

    A small fixed set of long-lived daemon worker threads pull tasks from a
    ``queue.Queue``. Concurrency — and the live thread count — stay capped at
    ``_AUTHORING_POOL_WORKERS`` no matter how large a request burst is: excess
    calls queue rather than each spawning its own thread.

    CPython ``ThreadPoolExecutor`` registers ``_python_exit``, which joins
    running workers even when they are daemon threads. A stalled LLM call
    using ``resolve_timeout()`` (3600s) would then block reload/shutdown for
    up to an hour after the caller already timed out. Raw daemon ``Thread``s
    without that atexit hook let the process exit; in-flight HTTP still
    cannot be cancelled from another thread.

    Each task runs inside a snapshot of the submitting thread's
    ``contextvars.copy_context()`` so the caller's LLM-attribution / request-id
    (``llm_service.attribution``) and ``trace_id``
    (``shared.observability.trace_context``) survive the thread hand-off — the
    same context-propagation contract ``shared.concurrency`` documents.

    Invariants:
        - At most ``_AUTHORING_POOL_WORKERS`` worker threads exist for this pool,
          and at most that many tasks run ``fn`` concurrently.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown = False
        for n in range(_AUTHORING_POOL_WORKERS):
            threading.Thread(
                target=self._worker_loop, name=f"studio-authoring-{n}", daemon=True
            ).start()

    def _worker_loop(self) -> None:
        """Pull and run queued tasks until a shutdown sentinel arrives.

        Preconditions:
            - Runs on a daemon thread started by ``__init__``.
        Postconditions:
            - Returns (thread exits) on the ``None`` sentinel; otherwise runs
              each task's ``fn`` inside its captured context, resolving its
              future. A task whose caller already timed out (future cancelled)
              is skipped rather than run.
        """
        while True:
            task = self._queue.get()
            if task is None:  # shutdown sentinel
                return
            fut, ctx, fn, args = task
            # A caller that timed out cancels its future (see ``_run``); skip a
            # task no waiter is blocked on rather than mutate registry state.
            # ``set_running_or_notify_cancel`` returns False iff cancelled.
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                result = ctx.run(fn, *args)
            except Exception as exc:
                fut.set_exception(exc)
            else:
                fut.set_result(result)

    def submit(self, fn: Callable[..., T], *args: object) -> concurrent.futures.Future[T]:
        """Enqueue ``fn(*args)`` for a worker thread, or fail if this pool is shut down.

        Preconditions:
            - ``fn`` is callable; ``args`` match ``fn``.
        Postconditions:
            - Returns a pending future a worker will resolve, or a completed
              future whose exception is ``RuntimeError`` when the pool is shut
              down. The submitting thread's context is snapshotted so the worker
              runs ``fn`` with the caller's attribution / trace contextvars.
        """
        fut: concurrent.futures.Future[T] = concurrent.futures.Future()
        with self._lock:
            if self._shutdown:
                fut.set_exception(RuntimeError("Agent Studio authoring executor is shut down"))
                return fut
            self._queue.put((fut, contextvars.copy_context(), fn, args))
        return fut

    def shutdown(self) -> None:
        """Reject new work and wake idle workers to exit. In-flight tasks are not joined.

        Preconditions:
            - None.
        Postconditions:
            - Subsequent ``submit`` calls complete with ``RuntimeError``.
            - Each worker receives a sentinel and exits once idle (a worker mid
              ``fn`` exits after that call returns; it is a daemon, so it never
              blocks interpreter exit).
            - Idempotent.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        for _ in range(_AUTHORING_POOL_WORKERS):
            self._queue.put(None)

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
        - Raises ``RuntimeError`` chained from ``concurrent.futures.TimeoutError``
          if ``fn`` does not finish within ``AUTHORING_TIMEOUT_S``. A task still
          queued at that point is cancelled so no worker runs it with no caller
          waiting; a task already running is left to finish (it cannot be
          cancelled once started).
    """
    fut = _authoring_executor().submit(fn, *args)
    try:
        return fut.result(timeout=AUTHORING_TIMEOUT_S)
    except concurrent.futures.TimeoutError as exc:
        # Best-effort: cancel() succeeds only while the task is still queued
        # (PENDING); the worker's set_running_or_notify_cancel then skips it.
        fut.cancel()
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
