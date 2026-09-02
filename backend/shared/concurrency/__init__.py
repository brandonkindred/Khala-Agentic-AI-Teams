"""Shared concurrency primitives used across agent teams.

Exports:

- :class:`BackgroundHeartbeat` — a single driver for the "daemon thread runs a
  callable on an interval until stopped" pattern that several teams had
  independently hand-rolled.
- :func:`flock_lock` — an exclusive, cross-process ``fcntl.flock`` on a
  well-known file: the primitive for "serialize access to a shared resource
  across worker *processes*", not just threads in one process.
- :class:`CloneLockAcquisitionError` — the exception ``held_checkout_lock``
  raises when ACQUIRING a checkout lock fails, so callers can tell that apart
  from any other ``OSError`` escaping their locked section.
- :func:`held_checkout_lock` — the cancellation-safe async wrapper around
  ``flock_lock`` for holding it from a coroutine (acquisition runs in an
  executor thread; a cancellation of the awaiting coroutine mid-acquisition
  must not leak the flock).
- :class:`KeyedLockManager` — a per-key mutual-exclusion registry: concurrent
  writers touching the same key are serialized, while writers touching
  disjoint keys proceed fully concurrently.
- :class:`LatestValueFlusher` — a single-slot mailbox + daemon writer thread that
  coalesces a burst of writes into one background write, for moving a slow,
  overwrite-semantics write off a thread that holds a lock other threads need.
- :func:`parallel_map` — a single, correct "bounded parallel map with contextvar
  propagation" helper, replacing the per-team ``ThreadPoolExecutor`` fan-outs.
"""

from __future__ import annotations

from shared.concurrency.checkout_lock import CloneLockAcquisitionError, held_checkout_lock
from shared.concurrency.flock_lock import flock_lock
from shared.concurrency.heartbeat import BackgroundHeartbeat
from shared.concurrency.keyed_lock_manager import KeyedLockManager
from shared.concurrency.latest_value_flusher import LatestValueFlusher
from shared.concurrency.parallel_map import parallel_map

__all__ = [
    "BackgroundHeartbeat",
    "CloneLockAcquisitionError",
    "KeyedLockManager",
    "LatestValueFlusher",
    "flock_lock",
    "held_checkout_lock",
    "parallel_map",
]
