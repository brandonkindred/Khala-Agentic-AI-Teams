"""Per-job event bus for SSE streaming (investment).

A thin team-local binding over the shared bus algorithm in
:mod:`shared_job_event_bus`. State is process-local to this module; pipeline
threads call :func:`publish`, SSE generators call :func:`subscribe` /
:func:`unsubscribe`. See :mod:`shared_job_event_bus` for the multi-worker caveat.

A background reaper (lazily started on the first :func:`subscribe`) bounds
in-memory growth: subscriptions that skip :func:`cleanup_job` — a crash, or an
SSE client that abandons its connection without its ``finally`` running — are
evicted once idle past :data:`_SUB_TTL_SECONDS`, with a hard cap of
:data:`_MAX_JOBS_TRACKED` tracked jobs. **Consumers MUST call**
:meth:`Subscription.touch` at least once per TTL while their stream is alive so
the reaper does not evict an actively connected client (see
:class:`shared_job_event_bus.ReaperHandle`).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from shared_env_config import env_int
from shared_job_event_bus import BusState, ReaperHandle, Subscription
from shared_job_event_bus import cleanup_job as _cleanup_job
from shared_job_event_bus import publish as _publish
from shared_job_event_bus import subscribe as _subscribe
from shared_job_event_bus import unsubscribe as _unsubscribe

logger = logging.getLogger(__name__)

__all__ = ["Subscription", "subscribe", "unsubscribe", "publish", "cleanup_job", "shutdown"]


# Idle subscriptions older than this are reaped. Strategy-lab runs can stay
# connected for a while; an hour absorbs slow/stalled runs but still bounds
# memory under pathological conditions (crash, abandoned SSE client).
_SUB_TTL_SECONDS: float = float(env_int("INVESTMENT_EVENT_BUS_TTL_SECONDS", 3600))
# Hard cap on tracked jobs. When exceeded, the oldest (by creation time) are
# evicted and their subscribers woken so they exit cleanly.
_MAX_JOBS_TRACKED: int = env_int("INVESTMENT_EVENT_BUS_MAX_JOBS", 1024)
# Reaper wake-up interval.
_REAPER_INTERVAL_SECONDS: float = float(env_int("INVESTMENT_EVENT_BUS_REAPER_INTERVAL", 300))

# This team's independent bus namespace. ``_lock``/``_subscribers`` are exposed
# (and alias the shared state) for tests and introspection.
_state = BusState()
_lock = _state.lock
_subscribers = _state.subscribers
_job_created_at = _state.job_created_at

# The TTL/cap are passed as callables over the module globals so tests can
# ``monkeypatch`` the tunables and have the very next reap honour them.
_reaper = ReaperHandle(
    _state,
    ttl_seconds=lambda: _SUB_TTL_SECONDS,
    max_jobs=lambda: _MAX_JOBS_TRACKED,
    interval_seconds=_REAPER_INTERVAL_SECONDS,
    name="investment-event-bus-reaper",
    label="investment event-bus",
    logger=logger,
)


def _start_reaper_if_needed() -> None:
    """Lazily start the reaper; idempotent and concurrency-safe (see :class:`ReaperHandle`)."""
    _reaper.ensure_started()


def _reap_once() -> None:
    """Single reaper pass (exposed for tests). Reads the current TTL/cap globals."""
    _reaper.reap_once()


def subscribe(job_id: str) -> Subscription:
    """Create a subscription for *job_id*. The caller must call :func:`unsubscribe` when done."""
    sub = _subscribe(_state, job_id)
    _start_reaper_if_needed()
    return sub


def unsubscribe(job_id: str, sub: Subscription) -> None:
    """Remove *sub* from *job_id*'s subscriber list."""
    _unsubscribe(_state, job_id, sub)


def publish(job_id: str, event: Dict[str, Any], *, event_type: Optional[str] = None) -> None:
    """Broadcast *event* to all subscribers of *job_id* (thread-safe)."""
    _publish(_state, job_id, event, event_type=event_type)


def cleanup_job(job_id: str) -> None:
    """Remove all subscribers for *job_id* (call after terminal event)."""
    _cleanup_job(_state, job_id)


def shutdown() -> None:
    """Stop the reaper thread (tests / lifespan); idempotent and re-startable."""
    _reaper.shutdown()
