"""Shared HTTP connection pooling for agent teams.

Every outbound HTTP caller in the codebase historically opened a fresh
``httpx.Client`` per request (``with httpx.Client(...) as client:``), which
discards the connection pool after a single round-trip.  On the hot paths —
the job service (called by every team) and the LLM clients — this causes
connection churn and needless object allocation under concurrency.

This module exposes a small set of process-wide, thread-safe pooled
``httpx.Client`` instances so callers reuse keep-alive connections instead of
re-establishing them.  Clients are bucketed by timeout so each call site keeps
its existing timeout semantics while still sharing one client per bucket.

Invariants:
    - Exactly one ``httpx.Client`` exists per (rounded) timeout bucket for the
      lifetime of the process (or until :func:`close_pool`).
    - All accessors are safe to call from multiple threads concurrently.
"""

from __future__ import annotations

import atexit
import math
import threading

import httpx

__all__ = ["get_pooled_client", "close_pool", "DEFAULT_LIMITS"]

# Bound concurrency so a burst of agent tasks cannot open unbounded sockets.
DEFAULT_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20)

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
