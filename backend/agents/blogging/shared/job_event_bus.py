"""Per-job event bus for SSE streaming (blogging).

A team-local binding over the shared bus algorithm in
:mod:`shared_job_event_bus`. Pipeline threads call :func:`publish` to broadcast
events; SSE endpoint generators call :func:`subscribe` / :func:`unsubscribe` to
receive them via a thread-safe deque.

.. warning::
   **Process-local state.** Subscribers are held in an in-memory dict for the
   lifetime of the hosting process. Under a multi-worker deployment
   (``uvicorn --workers N``) or multiple container replicas, events published
   on one worker will NOT reach SSE clients connected to another worker. Run
   blogging's API single-worker, or front it with sticky sessions, until this
   is migrated to a shared bus (Postgres ``LISTEN/NOTIFY`` or the team event
   bus in ``backend/agents/event_bus/``).

To bound in-memory growth under abnormal conditions (e.g. a crash that skips
:func:`cleanup_job`, or an SSE client that abandons its connection without
the ``finally`` block running), a background reaper evicts idle subscriptions
older than :data:`_SUB_TTL_SECONDS`, and a hard cap of
:data:`_MAX_JOBS_TRACKED` jobs triggers eviction of the oldest entries.

**Consumers MUST call** :meth:`Subscription.touch` at least once per
:data:`_SUB_TTL_SECONDS` (default 1h) while their stream is alive. The reaper
uses ``last_activity`` as its liveness signal, and publish-side activity is
not a reliable proxy — a legitimate job can go quiet for long stretches (e.g.
the SSE endpoint's 4-hour keepalive window, or the ghost-writer waiting on
human input). Evicting an actively connected consumer would cause later
terminal events to be dropped, so the contract is: if you're still reading,
touch the subscription.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from shared_concurrency import BackgroundHeartbeat
from shared_env_config import env_int
from shared_job_event_bus import BusState, Subscription
from shared_job_event_bus import cleanup_job as _cleanup_job
from shared_job_event_bus import publish as _publish
from shared_job_event_bus import reap_once as _reap_once_core
from shared_job_event_bus import subscribe as _subscribe
from shared_job_event_bus import unsubscribe as _unsubscribe

logger = logging.getLogger(__name__)

__all__ = ["Subscription", "subscribe", "unsubscribe", "publish", "cleanup_job", "shutdown"]


# Idle subscriptions older than this are reaped. Pipeline jobs run on the order
# of minutes; an hour is long enough to absorb slow/stalled jobs but short
# enough to bound memory under pathological conditions.
_SUB_TTL_SECONDS: float = float(env_int("BLOGGING_EVENT_BUS_TTL_SECONDS", 3600))
# Hard cap on tracked jobs. When exceeded, the oldest (by creation time) are
# evicted and their subscribers woken so they exit cleanly.
_MAX_JOBS_TRACKED: int = env_int("BLOGGING_EVENT_BUS_MAX_JOBS", 1024)
# Reaper wake-up interval.
_REAPER_INTERVAL_SECONDS: float = float(env_int("BLOGGING_EVENT_BUS_REAPER_INTERVAL", 300))

# This team's independent bus namespace. The module-level aliases expose the
# shared state for tests and the reaper; they reference the same objects, so
# in-place mutation by the shared algorithm is visible here.
_state = BusState()
_lock = _state.lock
_subscribers = _state.subscribers
_job_created_at = _state.job_created_at

_reaper: Optional[BackgroundHeartbeat] = None


def _start_reaper_if_needed() -> None:
    """Lazily start the reaper (calls ``_reap_once`` every ``_REAPER_INTERVAL_SECONDS``).

    The check-and-start runs under ``_lock`` so concurrent subscribes can't double-start
    and orphan a beater (idempotent). Spawning the thread under the lock is safe — it
    does no join, and the new beater's first ``_reap_once`` is a full interval away.
    """
    global _reaper
    with _lock:
        if _reaper is not None and _reaper.is_alive():
            return
        _reaper = BackgroundHeartbeat(
            _reap_once,
            _REAPER_INTERVAL_SECONDS,
            name="blogging-event-bus-reaper",
            join_timeout=2.0,
            on_error=lambda exc: logger.error(
                "blogging event-bus reaper iteration failed", exc_info=exc
            ),
        ).start()


def _reap_once() -> None:
    """Single reaper pass (exposed for tests). Reads the current TTL/cap globals.

    Indirecting through the module globals (rather than capturing them once)
    keeps the documented behaviour that tests can ``monkeypatch`` the tunables
    and have the very next reap honour them.
    """
    _reap_once_core(
        _state,
        ttl_seconds=_SUB_TTL_SECONDS,
        max_jobs=_MAX_JOBS_TRACKED,
        logger=logger,
        label="blogging event-bus",
    )


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
    """Stop the reaper thread (tests / lifespan); idempotent and re-startable.

    The global is swapped under ``_lock``, but ``stop()`` (which joins the beater,
    and ``_reap_once`` takes ``_lock``) runs *outside* the lock to avoid a deadlock.
    """
    global _reaper
    with _lock:
        reaper = _reaper
        _reaper = None
    if reaper is not None:
        reaper.stop()
