"""Shared per-job in-memory event bus for SSE streaming.

Re-exports the public API of the shared per-job in-memory event bus: the bus
algorithm from :mod:`shared.job_event_bus.bus`, the threaded reaper from
:mod:`shared.job_event_bus.reaper`, and the asyncio periodic scheduler from
:mod:`shared.job_event_bus.scheduler`. Teams hold their own :class:`BusState`
and bind thin module-level ``subscribe``/``publish`` wrappers over it; see the
module docstring in ``bus.py`` for the contract and the multi-worker caveat.
"""

from __future__ import annotations

from shared.job_event_bus.bus import (
    BusState,
    Subscription,
    cleanup_job,
    publish,
    reap_once,
    subscribe,
    unsubscribe,
)
from shared.job_event_bus.reaper import ReaperHandle
from shared.job_event_bus.scheduler import schedule_periodic_reap, stop_periodic_reap

__all__ = [
    "Subscription",
    "BusState",
    "subscribe",
    "unsubscribe",
    "publish",
    "cleanup_job",
    "reap_once",
    "ReaperHandle",
    "schedule_periodic_reap",
    "stop_periodic_reap",
]
