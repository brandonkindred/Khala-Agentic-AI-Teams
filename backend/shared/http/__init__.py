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
      loop)`` while that loop is alive; entries are dropped (and clients
      closed) when their owning loop is closed or garbage-collected.
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


def _sync_close_async_client(client: httpx.AsyncClient) -> None:
    """Best-effort sync teardown for a pooled ``httpx.AsyncClient``.

    Preconditions:
        - ``client`` is an ``httpx.AsyncClient``.
    Postconditions:
        - When no event loop is running, ``client`` is closed via
          ``asyncio.run(client.aclose())``.
        - When an event loop is already running, logs a warning and returns
          without closing (callers in async context must ``await client.aclose()``).
    """
    if client.is_closed:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(client.aclose())
    else:
        logger.warning(
            "Cannot sync-close pooled AsyncClient while event loop is running; "
            "leaving client in pool (use await client.aclose() in async context)"
        )


_async_lock = threading.Lock()


@dataclass
class _AsyncPoolEntry:
    """One pooled async client plus a weak reference to its owning event loop."""

    client: httpx.AsyncClient
    loop_ref: weakref.ref | None  # None for the no-running-loop sentinel key


# Keyed by (timeout bucket, id(running loop) or 0 when no loop is running).
_async_clients: dict[tuple[float, int], _AsyncPoolEntry] = {}


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the running event loop, or None if this thread has none."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _evict_async_pool_key(key: tuple[float, int]) -> None:
    """Remove and best-effort-close the pooled client for ``key`` (if present).

    Safe to call from a ``weakref.finalize`` callback after the owning loop is
    collected. Acquires ``_async_lock``.
    """
    with _async_lock:
        entry = _async_clients.pop(key, None)
    if entry is None:
        return
    try:
        _sync_close_async_client(entry.client)
    except Exception:  # noqa: BLE001 — best-effort teardown
        pass


def _purge_stale_async_clients_locked() -> None:
    """Drop entries whose owning loop is gone or closed. Caller holds ``_async_lock``."""
    stale: list[tuple[float, int]] = []
    for key, entry in _async_clients.items():
        if key[1] == 0:
            continue  # no-loop sentinel — only cleared by close_async_pool
        loop = entry.loop_ref() if entry.loop_ref is not None else None
        if loop is None or loop.is_closed():
            stale.append(key)
    for key in stale:
        entry = _async_clients.pop(key)
        try:
            _sync_close_async_client(entry.client)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


def get_pooled_async_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Return a shared, connection-pooled ``httpx.AsyncClient`` for ``timeout``.

    The client is created lazily on first use for a given ``(timeout bucket,
    running event loop)`` key and reused thereafter within that loop. Callers
    must NOT close the returned client or use it as a context manager — it is
    shared for that loop and closed when the loop ends or via
    :func:`close_async_pool` at shutdown.

    Preconditions:
        - ``timeout`` is a positive, finite number of seconds.
    Postconditions:
        - Returns a live (non-closed) ``httpx.AsyncClient`` configured with
          ``DEFAULT_LIMITS`` and the requested timeout.
        - Repeated calls with timeouts in the same bucket *on the same running
          event loop* return the *same* instance.
        - Calls from a different event loop (including sequential
          ``asyncio.run`` invocations) receive a distinct client, avoiding
          ``RuntimeError: Event loop is closed`` on keep-alive reuse.
        - Entries for closed/collected loops are evicted so sequential
          ``asyncio.run`` calls do not leak unbounded clients (and so a
          recycled ``id(loop)`` cannot resurrect a dead client's transport).
    """
    loop = _running_loop()
    loop_key = id(loop) if loop is not None else 0
    key = (_bucket(timeout), loop_key)
    with _async_lock:
        _purge_stale_async_clients_locked()
        entry = _async_clients.get(key)
        if entry is None or entry.client.is_closed:
            client = httpx.AsyncClient(timeout=timeout, limits=DEFAULT_LIMITS)
            loop_ref = weakref.ref(loop) if loop is not None else None
            _async_clients[key] = _AsyncPoolEntry(client=client, loop_ref=loop_ref)
            if loop is not None:
                # Evict when the loop object is garbage-collected (after
                # asyncio.run / explicit loop.close). Purge-on-access covers
                # the closed-but-not-yet-collected window.
                weakref.finalize(loop, _evict_async_pool_key, key)
        else:
            client = entry.client
        return client


def close_async_pool() -> None:
    """Close and drop all pooled async clients.

    Idempotent: safe to call when the pool is already empty. Intended for
    process shutdown and test teardown. Uses :func:`_sync_close_async_client`
    for best-effort teardown (including from ``atexit``).

    Postconditions:
        - Closed clients are removed from the pool; clients that could not be
          sync-closed (e.g. event loop already running) remain until a later
          close attempt, loop eviction, or replacement via
          :func:`get_pooled_async_client`.
        - When no event loop is running, the pool is empty after this call.
    """
    with _async_lock:
        remaining: dict[tuple[float, int], _AsyncPoolEntry] = {}
        for key, entry in _async_clients.items():
            try:
                _sync_close_async_client(entry.client)
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            if not entry.client.is_closed:
                remaining[key] = entry
        _async_clients.clear()
        _async_clients.update(remaining)


atexit.register(close_async_pool)
