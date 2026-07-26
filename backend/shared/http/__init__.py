"""Shared HTTP connection pooling for agent teams.

Every outbound HTTP caller in the codebase historically opened a fresh
``httpx.Client`` per request (``with httpx.Client(...) as client:``), which
discards the connection pool after a single round-trip.  On the hot paths —
the job service (called by every team) and the LLM clients — this causes
connection churn and needless object allocation under concurrency.

This module exposes a small set of process-wide, thread-safe pooled
``httpx.Client`` and ``httpx.AsyncClient`` instances so callers reuse keep-alive
connections instead of re-establishing them.  Sync clients are bucketed by
timeout; async clients are bucketed by ``(timeout, running event loop)`` so a
client's keep-alive transport is never reused after its owning loop is closed.
Both pools share ``DEFAULT_LIMITS`` and the same timeout-rounding scheme.

Invariants:
    - Exactly one ``httpx.Client`` exists per (rounded) timeout bucket for the
      lifetime of the process (or until :func:`close_pool`).
    - Exactly one ``httpx.AsyncClient`` exists per ``(timeout bucket, event
      loop)`` while that loop is alive. ``get_pooled_async_client`` requires a
      running loop (no sentinel / sync-setup bucket). Stale entries are closed
      then dropped when their owning loop is closed or garbage-collected. A
      client whose owning loop has stopped but is not yet closed stays pooled
      rather than being dropped open — there is no safe way to close it from
      another thread or loop (see ``_stopped_owner_loop_is_unclosable``), so it
      remains tracked and reported instead of silently leaking.
    - An entry is removed from the async pool before its client is closed, so
      teardown and a concurrent ``get_pooled_async_client`` can never disagree
      about which client is live; a client that fails to close is restored.
    - Exactly one ``weakref.finalize`` handle exists per live async pool entry;
      it is detached whenever the entry leaves or is replaced in the pool.
    - All accessors are safe to call from multiple threads concurrently.
    - Idle keep-alive sockets are recycled after ``keepalive_expiry`` seconds
      (``HTTP_KEEPALIVE_EXPIRY_S``, default ``15.0``) so the client drops them
      before an upstream/server closes an idle connection — otherwise reusing a
      server-closed socket raises ``RemoteProtocolError`` ("server disconnected
      without sending a response").
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import math
import os
import threading
import weakref
from dataclasses import dataclass

import httpx

from shared.http.retry import backoff_sleep, parse_retry_env_config, retry_delay

__all__ = [
    "get_pooled_client",
    "close_pool",
    "get_pooled_async_client",
    "close_async_pool",
    "aclose_async_pool",
    "DEFAULT_LIMITS",
    "backoff_sleep",
    "parse_retry_env_config",
    "retry_delay",
]

logger = logging.getLogger(__name__)

# httpx's default keepalive_expiry is 5s; we set an explicit, slightly larger
# value so the knob is visible and tunable. The ceiling stays well under the
# typical 60s idle timeout of upstreams/proxies so idle sockets are recycled
# before the far end closes them.
_DEFAULT_KEEPALIVE_EXPIRY_S = 15.0
# Floor: an expiry below ~1s recycles connections almost immediately, defeating
# pooling. A positive override below this is clamped up (rather than discarded).
_MIN_KEEPALIVE_EXPIRY_S = 1.0


def _keepalive_expiry_seconds() -> float:
    """Resolve the pool's idle keep-alive expiry from the environment.

    Preconditions:
        - None. Reads ``HTTP_KEEPALIVE_EXPIRY_S`` if set.
    Postconditions:
        - Returns a finite float ``>= _MIN_KEEPALIVE_EXPIRY_S`` (1.0s).
        - Unset / non-numeric / non-finite / non-positive values fall back to
          ``_DEFAULT_KEEPALIVE_EXPIRY_S``.
        - A positive value below the 1.0s floor is clamped up to the floor — an
          extremely short expiry would recycle sockets almost immediately and
          defeat the pool.
    """
    raw = os.getenv("HTTP_KEEPALIVE_EXPIRY_S")
    if raw is None:
        return _DEFAULT_KEEPALIVE_EXPIRY_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid HTTP_KEEPALIVE_EXPIRY_S=%r; using %s", raw, _DEFAULT_KEEPALIVE_EXPIRY_S)
        return _DEFAULT_KEEPALIVE_EXPIRY_S
    if not math.isfinite(value) or value <= 0:
        logger.warning("HTTP_KEEPALIVE_EXPIRY_S=%r out of range; using %s", raw, _DEFAULT_KEEPALIVE_EXPIRY_S)
        return _DEFAULT_KEEPALIVE_EXPIRY_S
    if value < _MIN_KEEPALIVE_EXPIRY_S:
        logger.warning(
            "HTTP_KEEPALIVE_EXPIRY_S=%r below %ss floor; clamping up",
            raw,
            _MIN_KEEPALIVE_EXPIRY_S,
        )
        return _MIN_KEEPALIVE_EXPIRY_S
    return value


# Bound concurrency so a burst of agent tasks cannot open unbounded sockets, and
# recycle idle keep-alive sockets before the far end closes them.
DEFAULT_LIMITS = httpx.Limits(
    max_connections=50,
    max_keepalive_connections=20,
    keepalive_expiry=_keepalive_expiry_seconds(),
)

_lock = threading.Lock()
_clients: dict[float, httpx.Client] = {}


def _bucket(timeout: float) -> float:
    """Round a timeout to a stable pool key.

    Preconditions:
        - ``timeout`` is a positive, finite number of seconds.
    Postconditions:
        - Returns a float usable as a dict key; equal inputs map to equal keys.
    """
    assert timeout > 0, f"timeout must be positive, got {timeout!r}"
    assert math.isfinite(timeout), f"timeout must be finite, got {timeout!r}"
    # Round to the nearest 0.5s so callers passing 30.0 vs 30.0001 share a client
    # while genuinely different timeouts (5s vs 30s) stay isolated.
    return round(float(timeout) * 2) / 2


def get_pooled_client(timeout: float = 30.0) -> httpx.Client:
    """Return a shared, connection-pooled ``httpx.Client`` for ``timeout``.

    The client is created lazily on first use for a given timeout bucket and
    reused thereafter.  Callers must NOT close the returned client or use it as
    a context manager — it is shared process-wide and closed via
    :func:`close_pool` at shutdown.

    Preconditions:
        - ``timeout`` is a positive, finite number of seconds.
    Postconditions:
        - Returns a live (non-closed) ``httpx.Client`` configured with
          ``DEFAULT_LIMITS`` and the requested timeout.
        - Repeated calls with timeouts in the same bucket return the *same*
          instance.
    """
    key = _bucket(timeout)
    # The lookup is done under the lock (rather than a lock-free fast path) so a
    # client cannot be closed by a concurrent close_pool() between the check and
    # the return. The lock is uncontended in the common case and its cost is
    # negligible next to the HTTP round-trip the client is about to make.
    with _lock:
        client = _clients.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(timeout=timeout, limits=DEFAULT_LIMITS)
            _clients[key] = client
        return client


def close_pool() -> None:
    """Close and drop all pooled clients.

    Idempotent: safe to call when the pool is already empty.  Intended for
    process shutdown and test teardown.

    Postconditions:
        - The pool is empty; subsequent :func:`get_pooled_client` calls create
          fresh clients.
    """
    with _lock:
        for client in _clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        _clients.clear()


atexit.register(close_pool)


# Reentrant by necessity, not convenience: allocating an ``httpx.AsyncClient``
# (or a ``weakref.finalize``) inside this lock can trigger a cyclic-GC pass that
# collects a dead event loop, which synchronously runs that loop's finalizer →
# ``_evict_async_pool_key`` → this same lock on this same thread. A plain Lock
# self-deadlocks there.
_async_lock = threading.RLock()


@dataclass
class _AsyncPoolEntry:
    """One pooled async client plus a weak reference to its owning event loop.

    ``finalizer`` is the ``weakref.finalize`` handle that evicts this entry when
    the owning loop is collected. It is detached whenever the entry leaves the
    pool so repeated create/close cycles on one loop cannot accumulate live
    finalizers in the weakref registry.
    """

    client: httpx.AsyncClient
    loop_ref: weakref.ref
    finalizer: weakref.finalize | None = None


# Keyed by (timeout bucket, id(running event loop)).
_async_clients: dict[tuple[float, int], _AsyncPoolEntry] = {}

# Bound on how many re-scan rounds ``aclose_async_pool`` performs to catch
# clients repooled by concurrent ``get_pooled_async_client`` calls during its
# await points. Teardown must terminate even under a caller that repools in a
# tight loop.
_ACLOSE_POOL_MAX_ROUNDS = 5

# Upper bound on waiting for a close submitted to another (running) loop. A
# wedged or saturated foreign loop must not hang process shutdown.
_CROSS_LOOP_CLOSE_TIMEOUT_S = 5.0

# Strong references to fire-and-forget aclose tasks scheduled on the caller's
# own loop. asyncio holds only a weak reference to a running task, so without
# this an unreferenced close can be garbage-collected before it completes.
_pending_aclose_tasks: set[asyncio.Task] = set()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the running event loop, or None if this thread has none."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _detach_finalizer(entry: _AsyncPoolEntry) -> None:
    """Detach ``entry``'s loop finalizer so it cannot fire or leak.

    Postconditions:
        - ``entry.finalizer`` is detached (a no-op if already dead) and cleared,
          so an entry that has left the pool holds no registry slot.
    """
    finalizer = entry.finalizer
    entry.finalizer = None
    if finalizer is not None:
        finalizer.detach()


def _take_async_pool_entry(key: tuple[float, int], entry: _AsyncPoolEntry) -> bool:
    """Remove ``entry`` from the pool, taking ownership of its teardown.

    Removing *before* closing (rather than after) is what makes teardown safe
    against a concurrent ``get_pooled_async_client``: once taken, no other
    caller can hand this client out, and a racing caller creates a fresh entry
    under the same key that the teardown loop picks up on its next round.

    Postconditions:
        - Returns True and detaches the finalizer if ``entry`` was still the
          pooled value for ``key``; returns False if another caller already
          replaced or removed it (in which case the caller must not close it).
    """
    with _async_lock:
        if _async_clients.get(key) is not entry:
            return False
        _async_clients.pop(key, None)
    _detach_finalizer(entry)
    return True


def _restore_async_pool_entry(key: tuple[float, int], entry: _AsyncPoolEntry) -> None:
    """Put a still-open ``entry`` back in the pool after a failed teardown.

    Postconditions:
        - ``entry`` is re-pooled (with a fresh finalizer) when ``key`` is still
          vacant.
        - When a newer entry already holds ``key``, that one wins and this
          client is closed instead of being silently orphaned — its finalizer
          was already detached by :func:`_take_async_pool_entry`, so nothing
          else would ever close or report it.
    """
    loop = entry.loop_ref()
    with _async_lock:
        vacant = key not in _async_clients
        if vacant:
            if loop is not None and not loop.is_closed():
                entry.finalizer = weakref.finalize(loop, _evict_async_pool_key, key)
            _async_clients[key] = entry
    if vacant:
        return
    # Displaced by a newer client: close this one rather than leak its transport.
    if not _close_client_on_owning_loop(entry):
        logger.warning("Displaced pooled AsyncClient could not be closed; its transport may stay open")


def _close_client_on_owning_loop(entry: _AsyncPoolEntry) -> bool:
    """Close ``entry.client`` on its owning event loop when that loop is live.

    Preconditions:
        - ``entry`` references the client to close.
    Postconditions:
        - If the owning loop is the *currently running* loop, schedules
          ``aclose()`` on it as a task (blocking here would deadlock) and
          returns True — meaning "safe to drop the entry", not "already
          closed"; the client closes asynchronously on this loop. Prefer
          :func:`aclose_async_pool` when the caller can ``await`` completion.
        - If the owning loop is a *different* loop that is still running, runs
          ``aclose()`` on that loop via ``run_coroutine_threadsafe`` (bounded
          by a 5s wait). Never runs ``asyncio.run(aclose)`` on a foreign loop —
          that is unsafe for keep-alive transports.
        - If the owning loop is *stopped but not closed* (e.g. after
          ``run_until_complete`` returns), returns False so the entry stays
          pooled. See :func:`_stopped_owner_loop_is_unclosable` for why the
          close cannot be performed from here.
        - If the owning loop is closed or collected, skips ``aclose`` (a
          coroutine cannot run there) and returns True so the pool can drop the
          entry without a false "closed" mark from a wrong-loop close.
        - Returns True when the client is closed, safe to drop, or the owning
          loop is gone.
    """
    client = entry.client
    if client.is_closed:
        return True
    loop = entry.loop_ref()
    if loop is None or loop.is_closed():
        # Transport was bound to a dead loop; do not aclose on a replacement loop.
        return True
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        # Same-thread running loop: cannot block on aclose here (deadlock).
        # Prefer ``await aclose_async_pool()`` when the caller can await.
        # Schedule aclose on this loop and drop the pool entry immediately —
        # returning True means "safe to remove", not "already closed".
        # Hold a strong reference until the task finishes: asyncio only keeps a
        # weak one, so an unreferenced task can be garbage-collected mid-flight.
        task = running.create_task(_aclose_quietly(client), name="shared.http.aclose")
        _pending_aclose_tasks.add(task)
        task.add_done_callback(_pending_aclose_tasks.discard)
        return True
    if not loop.is_running():
        return _stopped_owner_loop_is_unclosable()
    fut = None
    try:
        fut = asyncio.run_coroutine_threadsafe(_aclose_quietly(client), loop)
        fut.result(timeout=_CROSS_LOOP_CLOSE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — best-effort teardown
        # Stop the close from running later against an entry we may re-pool;
        # a half-finished aclose would hand callers a client httpx then rejects.
        if fut is not None:
            fut.cancel()
        logger.warning("Failed to aclose pooled AsyncClient on owning loop", exc_info=True)
    return client.is_closed


def _stopped_owner_loop_is_unclosable() -> bool:
    """Report that a client owned by a stopped-but-open loop cannot be closed here.

    A loop that is neither running nor closed still owns the client's keep-alive
    transport, so the close has to happen *on that loop* — but there is no safe
    way to make that happen from outside it:

    - ``run_coroutine_threadsafe`` queues the close on a loop nobody is driving,
      so awaiting it hangs forever.
    - ``run_until_complete`` requires the calling thread to have no running loop
      (so it is unusable from ``aclose_async_pool``), and even from a sync
      caller it would drive *another thread's* loop — draining that loop's ready
      queue on the wrong thread and reassigning its ``_thread_id``, racing the
      owning thread if it ever resumes.

    So the entry stays pooled rather than being dropped open: it is still
    tracked, still visible in ``aclose_async_pool``'s "left tracked" warning,
    and gets closed if the owning loop is ever run again or reaches
    :func:`aclose_async_pool` from inside itself. Callers that create their own
    loops should close them (``loop.close()``) or ``await aclose_async_pool()``
    from within the loop before abandoning it.

    Postconditions:
        - Always returns False ("not safe to drop"). Never raises.
    """
    logger.debug("Pooled AsyncClient owned by a stopped-but-open loop; keeping it tracked rather than dropping it")
    return False


async def _aclose_quietly(client: httpx.AsyncClient) -> None:
    """Await ``client.aclose()``, swallowing teardown errors."""
    if client.is_closed:
        return
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 — best-effort teardown
        logger.warning("Failed to aclose pooled AsyncClient", exc_info=True)


def _dispose_async_pool_entry(entry: _AsyncPoolEntry) -> bool:
    """Close ``entry.client`` on its owning loop; return True if safe to drop.

    Must not be called while holding ``_async_lock``.
    """
    try:
        return _close_client_on_owning_loop(entry)
    except Exception:  # noqa: BLE001 — best-effort teardown
        logger.warning("Failed to dispose pooled AsyncClient", exc_info=True)
        return entry.client.is_closed


def _evict_async_pool_key(key: tuple[float, int]) -> None:
    """Close (if owning loop still live) and remove the pooled client for ``key``.

    Safe to call from a ``weakref.finalize`` callback after the owning loop is
    collected. Only removes the entry when dispose reports it is safe to drop.
    """
    with _async_lock:
        entry = _async_clients.get(key)
    if entry is None:
        return
    if not _take_async_pool_entry(key, entry):
        return
    if not _dispose_async_pool_entry(entry):
        _restore_async_pool_entry(key, entry)


def _purge_stale_async_clients() -> None:
    """Drop entries whose owning loop is gone or closed.

    Must not be called while holding ``_async_lock``. Dead-loop entries are
    removed without foreign-loop ``aclose`` (see :func:`_close_client_on_owning_loop`).
    """
    with _async_lock:
        stale = [
            (key, entry)
            for key, entry in _async_clients.items()
            if (loop := entry.loop_ref()) is None or loop.is_closed()
        ]
    for key, entry in stale:
        if not _take_async_pool_entry(key, entry):
            continue
        if not _dispose_async_pool_entry(entry):
            _restore_async_pool_entry(key, entry)


def get_pooled_async_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Return a shared, connection-pooled ``httpx.AsyncClient`` for ``timeout``.

    The client is created lazily on first use for a given ``(timeout bucket,
    running event loop)`` key and reused thereafter within that loop. Callers
    must NOT close the returned client or use it as a context manager — it is
    shared for that loop and closed when the loop ends or via
    :func:`aclose_async_pool` / :func:`close_async_pool` at shutdown.

    Preconditions:
        - A running asyncio event loop in this thread (call from async code).
        - ``timeout`` is a positive, finite number of seconds.
    Postconditions:
        - Returns a live (non-closed) ``httpx.AsyncClient`` configured with
          ``DEFAULT_LIMITS`` and the requested timeout.
        - Repeated calls with timeouts in the same bucket *on the same running
          event loop* return the *same* instance.
        - Calls from a different event loop (including sequential
          ``asyncio.run`` invocations) receive a distinct client, avoiding
          ``RuntimeError: Event loop is closed`` on keep-alive reuse.
        - Entries for closed/collected loops are dropped (without foreign-loop
          ``aclose``) so sequential ``asyncio.run`` calls do not leak entries or
          resurrect a dead transport via a recycled ``id(loop)``.
    """
    loop = _running_loop()
    assert loop is not None, (
        "get_pooled_async_client requires a running event loop; "
        "call it from async code (e.g. via async_post_json / async_get_json)"
    )
    key = (_bucket(timeout), id(loop))
    _purge_stale_async_clients()
    with _async_lock:
        entry = _async_clients.get(key)
        if entry is None or entry.client.is_closed:
            if entry is not None:
                # Replacing a closed client: retire its finalizer instead of
                # stacking a second one on the same loop for the same key.
                _detach_finalizer(entry)
            client = httpx.AsyncClient(timeout=timeout, limits=DEFAULT_LIMITS)
            new_entry = _AsyncPoolEntry(client=client, loop_ref=weakref.ref(loop))
            new_entry.finalizer = weakref.finalize(loop, _evict_async_pool_key, key)
            _async_clients[key] = new_entry
        else:
            client = entry.client
        return client


async def aclose_async_pool(*, only_current_loop: bool = False) -> None:
    """Await-close pooled async clients on their owning loops.

    Prefer this from async code when teardown must finish before continuing.
    Clients owned by the current running loop are ``await``-ed directly; clients
    owned by other *running* loops are closed via ``run_coroutine_threadsafe``,
    bounded by ``_CROSS_LOOP_CLOSE_TIMEOUT_S`` so a wedged foreign loop cannot
    hang shutdown. A client owned by a stopped-but-open loop stays pooled (it
    cannot be closed safely from here); only dead/collected loops are dropped
    without ``aclose``.

    Each entry is removed from the pool *before* being closed, so a concurrent
    ``get_pooled_async_client`` during an await point cannot have its fresh
    client silently discarded; the loop re-scans for such repooled clients up to
    ``_ACLOSE_POOL_MAX_ROUNDS`` times.

    Preconditions:
        - Called from within a running event loop.
    Postconditions:
        - When ``only_current_loop`` is False (the default), every live pooled
          client is targeted, regardless of which loop owns it — the original
          whole-pool teardown behavior.
        - When ``only_current_loop`` is True, only entries owned by the
          calling loop are targeted; entries owned by other loops (including
          other concurrently *running* loops) are left completely untouched —
          not closed, not counted toward the "left tracked" warning. Use this
          from a coroutine sharing the pool with unrelated concurrent loops
          (e.g. multiple offloaded ``asyncio.run`` calls each on their own
          worker thread), where closing a foreign loop's client could sever an
          in-flight request that loop is still making.
        - Successfully closed and (for the targeted scope) dead-loop entries
          are removed from the pool.
        - Clients that remain open after a failed teardown stay tracked.
    """
    running = asyncio.get_running_loop()

    def _targeted(items):
        if only_current_loop:
            return [(key, entry) for key, entry in items if entry.loop_ref() is running]
        return list(items)

    for _round in range(_ACLOSE_POOL_MAX_ROUNDS):
        with _async_lock:
            snapshot = _targeted(_async_clients.items())
        if not snapshot:
            return
        for key, entry in snapshot:
            if not _take_async_pool_entry(key, entry):
                continue
            # try/finally, not except: a CancelledError here (shutdown racing
            # teardown) must still put the already-popped entry back, or its
            # open client becomes untrackable by any later teardown or purge.
            dropped = False
            try:
                dropped = await _aclose_entry(entry, running)
            finally:
                if not dropped:
                    _restore_async_pool_entry(key, entry)
        with _async_lock:
            still_targeted = _targeted(_async_clients.items())
        # A round that closed nothing and saw nothing repooled will not do
        # better on the next pass — stop instead of spinning.
        if {key: id(entry) for key, entry in still_targeted} == {key: id(entry) for key, entry in snapshot}:
            break
    with _async_lock:
        remaining = len(_targeted(_async_clients.items()))
    if remaining:
        logger.warning("aclose_async_pool left %d pooled AsyncClient(s) tracked after teardown", remaining)


async def _aclose_entry(entry: _AsyncPoolEntry, running: asyncio.AbstractEventLoop) -> bool:
    """Close one taken-ownership pool entry; return True if safe to drop.

    Preconditions:
        - ``entry`` has already been removed from the pool by the caller.
        - ``running`` is the caller's running event loop.
    Postconditions:
        - Returns True when the client is closed or its owning loop is dead
          (nothing left to close); False when the client is still open and the
          entry should be restored to the pool for a later attempt — including
          a stopped-but-open owning loop (see
          :func:`_stopped_owner_loop_is_unclosable`).
        - Never raises an ordinary exception. ``CancelledError`` (a
          ``BaseException``) can still propagate from the awaits; the caller
          restores the entry in a ``finally`` for exactly that reason.
    """
    client = entry.client
    if client.is_closed:
        return True
    loop = entry.loop_ref()
    if loop is None or loop.is_closed():
        # Transport bound to a dead loop; closing it from here is unsafe.
        return True
    try:
        if loop is running:
            await _aclose_quietly(client)
        elif loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(_aclose_quietly(client), loop)
            wrapped = asyncio.wrap_future(fut)
            try:
                await asyncio.wait_for(wrapped, timeout=_CROSS_LOOP_CLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                # A wedged foreign loop must not hang shutdown.
                fut.cancel()
                logger.warning("Timed out closing pooled AsyncClient on foreign loop")
        else:
            return _stopped_owner_loop_is_unclosable()
    except Exception:  # noqa: BLE001 — best-effort teardown
        logger.warning("Failed to aclose pooled AsyncClient", exc_info=True)
    return client.is_closed


def close_async_pool() -> None:
    """Best-effort sync teardown of pooled async clients (atexit / sync tests).

    Closes each client on its *owning* live loop via
    ``run_coroutine_threadsafe``. When called from that owning loop's thread,
    schedules ``aclose`` as a task (cannot block without deadlock) — prefer
    :func:`aclose_async_pool` from async code when you need awaited completion.
    A client owned by a stopped-but-open loop stays pooled; only dead-loop
    entries are dropped without ``aclose``.

    Entries are taken out of the pool before being closed so a concurrent
    ``get_pooled_async_client`` cannot hand out a client this teardown is about
    to close; a still-open client is restored to the pool afterwards.

    Postconditions:
        - Dead-loop and successfully closed entries are removed.
        - Clients that remain open stay tracked for a later close/purge.
    """
    with _async_lock:
        snapshot = list(_async_clients.items())
    for key, entry in snapshot:
        if not _take_async_pool_entry(key, entry):
            continue
        if not _dispose_async_pool_entry(entry):
            _restore_async_pool_entry(key, entry)


atexit.register(close_async_pool)
