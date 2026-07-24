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
      then dropped when their owning loop is closed or garbage-collected.
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

__all__ = [
    "get_pooled_client",
    "close_pool",
    "get_pooled_async_client",
    "close_async_pool",
    "aclose_async_pool",
    "DEFAULT_LIMITS",
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


_async_lock = threading.Lock()


@dataclass
class _AsyncPoolEntry:
    """One pooled async client plus a weak reference to its owning event loop."""

    client: httpx.AsyncClient
    loop_ref: weakref.ref


# Keyed by (timeout bucket, id(running event loop)).
_async_clients: dict[tuple[float, int], _AsyncPoolEntry] = {}


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the running event loop, or None if this thread has none."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _close_client_on_owning_loop(entry: _AsyncPoolEntry) -> bool:
    """Close ``entry.client`` on its owning event loop when that loop is live.

    Preconditions:
        - ``entry`` references the client to close.
    Postconditions:
        - If the owning loop is still alive, ``aclose()`` is run on *that* loop
          (via ``run_coroutine_threadsafe``, or ``await`` when called through
          :func:`aclose_async_pool`). Never runs ``asyncio.run(aclose)`` on a
          foreign loop — that is unsafe for keep-alive transports.
        - If the owning loop is already closed or collected, skips ``aclose``
          (the transport died with the loop) and returns True so the pool can
          drop the entry without a false "closed" mark from a wrong-loop close.
        - Returns True when the client is closed or the owning loop is gone.
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
        running.create_task(_aclose_quietly(client), name="shared.http.aclose")
        return True
    try:
        fut = asyncio.run_coroutine_threadsafe(_aclose_quietly(client), loop)
        fut.result(timeout=5.0)
    except Exception:  # noqa: BLE001 — best-effort teardown
        logger.warning("Failed to aclose pooled AsyncClient on owning loop", exc_info=True)
    return client.is_closed


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
    if not _dispose_async_pool_entry(entry):
        return
    with _async_lock:
        if _async_clients.get(key) is entry:
            _async_clients.pop(key, None)


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
        if not _dispose_async_pool_entry(entry):
            continue
        with _async_lock:
            if _async_clients.get(key) is entry:
                _async_clients.pop(key, None)


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
            client = httpx.AsyncClient(timeout=timeout, limits=DEFAULT_LIMITS)
            _async_clients[key] = _AsyncPoolEntry(client=client, loop_ref=weakref.ref(loop))
            weakref.finalize(loop, _evict_async_pool_key, key)
        else:
            client = entry.client
        return client


async def aclose_async_pool() -> None:
    """Await-close all pooled async clients on their owning loops.

    Prefer this from async code when teardown must finish before continuing.
    Clients owned by the current running loop are ``await``-ed directly; clients
    owned by other live loops are closed via ``run_coroutine_threadsafe``.
    Entries whose owning loop is already dead are dropped without ``aclose``.

    Postconditions:
        - Successfully closed / dead-loop entries are removed from the pool.
        - Clients that remain open after a failed teardown stay tracked.
    """
    with _async_lock:
        snapshot = list(_async_clients.items())
    running = asyncio.get_running_loop()
    for key, entry in snapshot:
        loop = entry.loop_ref()
        if entry.client.is_closed or loop is None or loop.is_closed():
            with _async_lock:
                if _async_clients.get(key) is entry:
                    _async_clients.pop(key, None)
            continue
        try:
            if loop is running:
                await _aclose_quietly(entry.client)
            else:
                fut = asyncio.run_coroutine_threadsafe(_aclose_quietly(entry.client), loop)
                await asyncio.wrap_future(fut)
        except Exception:  # noqa: BLE001 — best-effort teardown
            logger.warning("Failed to aclose pooled AsyncClient", exc_info=True)
        if entry.client.is_closed:
            with _async_lock:
                if _async_clients.get(key) is entry:
                    _async_clients.pop(key, None)


def close_async_pool() -> None:
    """Best-effort sync teardown of pooled async clients (atexit / sync tests).

    Closes each client on its *owning* live loop via
    ``run_coroutine_threadsafe``. When called from that owning loop's thread,
    schedules ``aclose`` as a task (cannot block without deadlock) — prefer
    :func:`aclose_async_pool` from async code when you need awaited completion.
    Dead-loop entries are dropped without foreign-loop ``aclose``.

    Postconditions:
        - Dead-loop and successfully closed entries are removed.
        - Clients that remain open stay tracked for a later close/purge.
    """
    with _async_lock:
        snapshot = list(_async_clients.items())
    for key, entry in snapshot:
        if _dispose_async_pool_entry(entry):
            with _async_lock:
                if _async_clients.get(key) is entry:
                    _async_clients.pop(key, None)


atexit.register(close_async_pool)
