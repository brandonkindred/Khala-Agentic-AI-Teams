"""Run a coroutine to completion from synchronous code.

Shared by ``orchestrator.py`` (thread-mode team runs) and
``adapters/market_research.py`` (its synchronous wrapper), both of which need
to call into async code from a synchronous entry point that may or may not
already be running inside an event loop (e.g. a Temporal activity vs. a plain
thread-pool worker).

Invariants:
    Never calls ``asyncio.run`` while a loop is already running in the
    calling thread — that would raise ``RuntimeError``. The offload pool
    exists solely to give such a call a fresh thread with no running loop.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import os
from typing import Awaitable, Optional, TypeVar

from shared.concurrency import LazySingleton
from shared.env_config import env_int
from shared.http import aclose_async_pool

__all__ = ["run_coroutine"]

_T = TypeVar("_T")


def _offload_pool_workers() -> int:
    """Worker cap for the lazy offload pool (env-tunable, clamped to >= 1).

    Default of 4 avoids serializing concurrent offloaded runs (e.g. multiple
    async Temporal activities on the same loop each calling ``run_coroutine``)
    behind a single worker.
    """
    return env_int("BRANDING_RUN_CORO_OFFLOAD_WORKERS", 4, floor=1)


_offload_pool: LazySingleton[concurrent.futures.ThreadPoolExecutor] = LazySingleton()
# Mirrors whatever ThreadPoolExecutor _offload_pool currently holds, so
# _reset_offload_pool_after_fork can unregister its atexit callback without a
# public "peek" API on LazySingleton. Written only inside _build_offload_pool,
# which LazySingleton.get_or_create already serializes to run at most once per
# _offload_pool instance — this is not a second hand-rolled lock, just a plain
# assignment inside code that's already serialized.
_constructed_offload_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _build_offload_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Factory for ``_offload_pool.get_or_create`` — builds the pool and registers its atexit shutdown."""
    global _constructed_offload_pool
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=_offload_pool_workers(), thread_name_prefix="branding-run-coroutine"
    )
    # Best-effort cleanup on interpreter exit. Threads in this pool only run
    # briefly per offloaded coroutine (see run_coroutine), so this should not
    # delay shutdown in practice; wait=False avoids blocking exit on a stuck run.
    atexit.register(pool.shutdown, wait=False)
    _constructed_offload_pool = pool
    return pool


def _get_offload_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Return (lazily creating) the shared pool used to offload ``run_coroutine`` calls.

    Postconditions:
        Returns the same ``ThreadPoolExecutor`` on every call after the first;
        registers an ``atexit`` shutdown the first time it is created (see
        ``_build_offload_pool``). Concurrent first calls race safely —
        ``LazySingleton.get_or_create`` serializes them under its own internal
        lock — so exactly one pool is built and every caller observes it.

    Note:
        Created lazily on first use rather than at module import time: worker
        threads don't survive ``os.fork()``, so eagerly creating them at import
        time risks a forked child (e.g. a multi-worker deployment that forks
        after import) inheriting a pool object whose threads don't actually
        exist in the child. Lazy creation means the pool is built after any
        such fork, in whichever process first calls ``run_coroutine`` from a
        thread with a running loop.
    """
    return _offload_pool.get_or_create(_build_offload_pool)


def _reset_offload_pool_after_fork() -> None:
    """Drop the offload pool inherited by a freshly forked child.

    A ``ThreadPoolExecutor``'s worker threads do not survive ``os.fork()``
    (only the forking thread continues in the child), so if the pool was
    already created in the parent before a fork, the child would otherwise
    inherit a reference to threads that don't exist there. Registering this
    via ``os.register_at_fork`` closes that gap on top of lazy creation:
    replacing ``_offload_pool`` with a fresh, unconstructed ``LazySingleton``
    makes the child lazily build its own fresh pool the next time
    ``run_coroutine`` needs to offload.

    Also unregisters the parent pool's ``atexit`` shutdown callback (a stale
    reference to a pool that doesn't exist in this process) so process exit in
    the child doesn't invoke it; the fresh pool the child eventually builds
    registers its own via ``_build_offload_pool``.
    """
    global _offload_pool, _constructed_offload_pool
    if _constructed_offload_pool is not None:
        atexit.unregister(_constructed_offload_pool.shutdown)
        _constructed_offload_pool = None
    _offload_pool = LazySingleton()


if hasattr(os, "register_at_fork"):  # POSIX only; no-op on platforms without fork.
    os.register_at_fork(after_in_child=_reset_offload_pool_after_fork)


async def _run_with_pool_cleanup(coroutine: Awaitable[_T]) -> _T:
    """Await *coroutine*, then close any async HTTP clients it pooled on this loop.

    ``asyncio.run`` (used by both branches of :func:`run_coroutine`) always
    creates a private, single-use event loop that is closed immediately after
    this coroutine returns. ``shared.http.get_pooled_async_client`` pools its
    ``httpx.AsyncClient`` instances per running loop, and a client cannot be
    closed once its owning loop is dead (see ``shared.http``'s pool
    invariants) — so without this cleanup, any pooled async client *coroutine*
    creates (e.g. via ``async_post_json``) leaks its transport when the
    ephemeral loop closes. Closing here, while the loop is still running and
    guaranteed to run no other coroutine afterward, is always safe.

    The cleanup is scoped to *this* loop (``only_current_loop=True``): the
    offload path in :func:`run_coroutine` can have several ``run_coroutine``
    calls executing concurrently, each on its own worker thread with its own
    ephemeral loop. An unscoped ``aclose_async_pool()`` closes every pooled
    client process-wide, including ones owned by those other still-running
    loops — severing an unrelated concurrent request mid-flight. Scoping to
    the current loop closes only what *this* call created.

    Postconditions:
        Returns *coroutine*'s result, or propagates whatever it raises;
        ``aclose_async_pool`` runs in a ``finally`` so cleanup happens on
        both paths.
    """
    try:
        return await coroutine
    finally:
        await aclose_async_pool(only_current_loop=True)


def run_coroutine(coroutine: Awaitable[_T]) -> _T:
    """Run *coroutine* to completion from synchronous code.

    Uses ``asyncio.run`` when no loop runs in this thread; otherwise drives it on
    a shared worker thread (see ``_get_offload_pool``) so we never call
    ``asyncio.run`` inside an active loop.

    Preconditions:
        ``coroutine`` is an un-awaited coroutine/awaitable. When called from a thread
        that already has a running loop, ``coroutine`` MUST NOT depend on objects
        bound to that loop (e.g. an ``asyncio.Queue`` or lock created on it): the
        offload path runs it on a *new* event loop in another thread, so
        loop-bound objects would fail. Callers (``graph.invoke_async`` and
        ``_gather_integrations`` in orchestrator.py; ``request_market_research_async``
        in adapters/market_research.py) allocate their own primitives, so they
        are safe.
    Postconditions:
        Returns the coroutine's result or propagates whatever it raises; never
        calls ``asyncio.run`` while a loop is already running in this thread.
        Any ``shared.http`` pooled async client created while running
        *coroutine* is closed before the ephemeral event loop closes (see
        ``_run_with_pool_cleanup``).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    # asyncio.get_running_loop() only ever returns a *running* loop (it raises
    # RuntimeError otherwise), so a plain None-check is sufficient here.
    if loop is not None:
        return _get_offload_pool().submit(asyncio.run, _run_with_pool_cleanup(coroutine)).result()
    return asyncio.run(_run_with_pool_cleanup(coroutine))
