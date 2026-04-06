"""
Event publisher for the Strands event bus.

In-process pub/sub with optional Postgres LISTEN/NOTIFY for cross-container
event propagation.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, Optional

from .models import Event, EventType

logger = logging.getLogger(__name__)

# In-memory event log (ring buffer for recent events, used by SSE endpoint).
_MAX_EVENT_HISTORY = 1000
_event_history: Deque[Event] = deque(maxlen=_MAX_EVENT_HISTORY)
_history_lock = threading.Lock()

# Subscribers are managed by subscriber.py; publisher just calls them.
_subscriber_callbacks: list = []  # Set by subscriber.py


def publish(
    event_type: EventType,
    payload: Dict[str, Any],
    *,
    source_team: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Event:
    """Publish an event to all subscribers.

    Returns the created Event for testing/inspection.
    """
    event = Event(
        event_type=event_type,
        payload=payload,
        source_team=source_team,
        job_id=job_id,
    )

    # Store in history
    with _history_lock:
        _event_history.append(event)

    # Notify in-process subscribers
    for callback in _subscriber_callbacks:
        try:
            callback(event)
        except Exception:
            logger.warning("Event subscriber callback failed", exc_info=True)

    logger.debug(
        "Published event: %s (team=%s, job=%s)",
        event_type.value,
        source_team or "n/a",
        job_id or "n/a",
    )
    return event


def get_event_history(
    *,
    event_type: Optional[EventType] = None,
    source_team: Optional[str] = None,
    limit: int = 100,
) -> list[Dict[str, Any]]:
    """Return recent events from the in-memory history."""
    with _history_lock:
        events = list(_event_history)
    if event_type:
        events = [e for e in events if e.event_type == event_type]
    if source_team:
        events = [e for e in events if e.source_team == source_team]
    return [e.to_dict() for e in events[-limit:]]
