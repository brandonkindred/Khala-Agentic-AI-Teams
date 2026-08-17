"""Shared concurrency primitives used across agent teams.

Exports:

- :class:`BackgroundHeartbeat` — a single driver for the "daemon thread runs a
  callable on an interval until stopped" pattern that several teams had
  independently hand-rolled.
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

from shared.concurrency.heartbeat import BackgroundHeartbeat
from shared.concurrency.keyed_lock_manager import KeyedLockManager
from shared.concurrency.latest_value_flusher import LatestValueFlusher
from shared.concurrency.parallel_map import parallel_map

__all__ = ["BackgroundHeartbeat", "KeyedLockManager", "LatestValueFlusher", "parallel_map"]
