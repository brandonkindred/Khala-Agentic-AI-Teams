"""Shared per-job in-memory event bus for SSE streaming.

Re-exports the bus algorithm from :mod:`shared_job_event_bus.bus`. Teams hold
their own :class:`BusState` and bind thin module-level ``subscribe``/``publish``
wrappers over it; see the module docstring in ``bus.py`` for the contract and
the multi-worker caveat.
"""

from __future__ import annotations

from shared_job_event_bus.bus import (
    BusState,
    Subscription,
    cleanup_job,
    publish,
    reap_once,
    subscribe,
    unsubscribe,
)
from shared_job_event_bus.reaper import ReaperHandle

__all__ = [
    "Subscription",
    "BusState",
    "subscribe",
    "unsubscribe",
    "publish",
    "cleanup_job",
    "reap_once",
    "ReaperHandle",
]
