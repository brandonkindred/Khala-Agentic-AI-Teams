"""
Event subscriber for the Strands event bus.

In-process subscription with callback-based event delivery.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import publisher
from .models import Event, EventType

logger = logging.getLogger(__name__)

# Subscriber registry: list of (filter, callback) tuples.
_subscribers: list[tuple[Optional[EventType], Callable[[Event], Any]]] = []


def subscribe(
    event_type: Optional[EventType],
    callback: Callable[[Event], Any],
) -> None:
    """Subscribe to events. If event_type is None, receives all events.

    The callback is called synchronously in the publisher's thread.
    For async processing, the callback should enqueue work rather than
    blocking.
    """
    _subscribers.append((event_type, callback))

    # Register dispatch function with publisher (once)
    if _dispatch not in publisher._subscriber_callbacks:
        publisher._subscriber_callbacks.append(_dispatch)

    logger.info(
        "Subscribed to events: %s",
        event_type.value if event_type else "all",
    )


def unsubscribe(callback: Callable[[Event], Any]) -> None:
    """Remove a callback from the subscriber list."""
    global _subscribers
    _subscribers = [(et, cb) for et, cb in _subscribers if cb is not callback]


def _dispatch(event: Event) -> None:
    """Internal dispatcher called by publisher for each event."""
    for event_filter, callback in _subscribers:
        if event_filter is None or event.event_type == event_filter:
            try:
                callback(event)
            except Exception:
                logger.warning(
                    "Subscriber callback failed for event %s",
                    event.event_type.value,
                    exc_info=True,
                )
