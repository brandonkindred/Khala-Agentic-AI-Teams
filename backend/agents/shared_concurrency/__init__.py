"""Shared concurrency primitives used across agent teams.

Exports:

- :class:`BackgroundHeartbeat` — a single driver for the "daemon thread runs a
  callable on an interval until stopped" pattern that several teams had
  independently hand-rolled.
- :func:`parallel_map` — a single, correct "bounded parallel map with contextvar
  propagation" helper, replacing the per-team ``ThreadPoolExecutor`` fan-outs.
"""

from __future__ import annotations

from shared_concurrency.heartbeat import BackgroundHeartbeat
from shared_concurrency.parallel_map import parallel_map

__all__ = ["BackgroundHeartbeat", "parallel_map"]
