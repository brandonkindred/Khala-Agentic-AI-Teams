"""
Lightweight event bus for cross-team coordination.

Supports in-process pub/sub (immediate) and Postgres LISTEN/NOTIFY
(when a database connection is available). Teams publish events when
job state changes or artifacts are produced; other teams or the
frontend can subscribe to react in real time.

Usage::

    from event_bus import publish, subscribe, EventType

    # Publishing
    publish(EventType.JOB_COMPLETED, {"team": "blogging", "job_id": "abc123"})

    # Subscribing
    subscribe(EventType.JOB_COMPLETED, lambda event: print(event))
"""

from .models import Event, EventType
from .publisher import publish
from .subscriber import subscribe, unsubscribe

__all__ = [
    "Event",
    "EventType",
    "publish",
    "subscribe",
    "unsubscribe",
]
