"""Per-job event bus for SSE streaming (investment).

A thin team-local binding over the shared bus algorithm in
:mod:`shared_job_event_bus`. State is process-local to this module; investment
SSE streams are short-lived and explicitly torn down via :func:`cleanup_job`,
so no background reaper is started here (reaping is opt-in per team).

Pipeline threads call :func:`publish`; SSE generators call :func:`subscribe` /
:func:`unsubscribe`. See :mod:`shared_job_event_bus` for the multi-worker caveat.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from shared_job_event_bus import BusState, Subscription
from shared_job_event_bus import cleanup_job as _cleanup_job
from shared_job_event_bus import publish as _publish
from shared_job_event_bus import subscribe as _subscribe
from shared_job_event_bus import unsubscribe as _unsubscribe

__all__ = ["Subscription", "subscribe", "unsubscribe", "publish", "cleanup_job"]

# This team's independent bus namespace. ``_lock``/``_subscribers`` are exposed
# (and alias the shared state) for tests and introspection.
_state = BusState()
_lock = _state.lock
_subscribers = _state.subscribers


def subscribe(job_id: str) -> Subscription:
    """Create a subscription for *job_id*. The caller must call :func:`unsubscribe` when done."""
    return _subscribe(_state, job_id)


def unsubscribe(job_id: str, sub: Subscription) -> None:
    """Remove *sub* from *job_id*'s subscriber list."""
    _unsubscribe(_state, job_id, sub)


def publish(job_id: str, event: Dict[str, Any], *, event_type: Optional[str] = None) -> None:
    """Broadcast *event* to all subscribers of *job_id* (thread-safe)."""
    _publish(_state, job_id, event, event_type=event_type)


def cleanup_job(job_id: str) -> None:
    """Remove all subscribers for *job_id* (call after terminal event)."""
    _cleanup_job(_state, job_id)
