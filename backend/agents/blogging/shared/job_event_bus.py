"""Per-job event bus for SSE streaming.

Pipeline threads call :func:`publish` to broadcast events; SSE endpoint generators
call :func:`subscribe` / :func:`unsubscribe` to receive them via a thread-safe deque.

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
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default


# Idle subscriptions older than this are reaped. Pipeline jobs run on the order
# of minutes; an hour is long enough to absorb slow/stalled jobs but short
# enough to bound memory under pathological conditions.
_SUB_TTL_SECONDS: float = float(_env_int("BLOGGING_EVENT_BUS_TTL_SECONDS", 3600))
# Hard cap on tracked jobs. When exceeded, the oldest (by creation time) are
# evicted and their subscribers woken so they exit cleanly.
_MAX_JOBS_TRACKED: int = _env_int("BLOGGING_EVENT_BUS_MAX_JOBS", 1024)
# Reaper wake-up interval.
_REAPER_INTERVAL_SECONDS: float = float(_env_int("BLOGGING_EVENT_BUS_REAPER_INTERVAL", 300))


@dataclass
class Subscription:
    """Handle returned by :func:`subscribe`.

    ``created_at`` is set on construction and never changes. ``last_activity``
    is the liveness signal used by the reaper: consumers should call
    :meth:`touch` at least once per :data:`_SUB_TTL_SECONDS` while their
    stream is alive. :func:`publish` also refreshes it on each delivered
    event, but publish-side activity alone is not sufficient — legitimate
    quiet periods (SSE keepalives during a slow job, ghost-writer waiting on
    human input) must not trigger eviction.
    """

    notify: threading.Event = field(default_factory=threading.Event)
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        """Refresh the liveness timestamp. Consumers call this each loop iteration.

        Cheap (single attribute write; no lock needed — CPython attribute
        assignment is atomic, and a stale read from the reaper is harmless:
        it will re-check on the next interval).
        """
        self.last_activity = time.monotonic()


_lock = threading.Lock()
_subscribers: Dict[str, list[Subscription]] = {}
# Creation time per job_id, used for LRU-style eviction when the global cap is
# exceeded. Kept in insertion order (Python dict guarantee from 3.7+).
_job_created_at: Dict[str, float] = {}

_reaper_thread: Optional[threading.Thread] = None
_reaper_stop = threading.Event()


def _start_reaper_if_needed() -> None:
    """Start the reaper daemon thread on first subscription (lazy init)."""
    global _reaper_thread
    if _reaper_thread is not None and _reaper_thread.is_alive():
        return
    # Holding _lock isn't strictly needed here (daemon init is idempotent-ish
    # and Python assignment is atomic), but we pay for one acquire to avoid a
    # double-start race during a burst of concurrent subscribes.
    thread = threading.Thread(
        target=_reaper_loop,
        name="blogging-event-bus-reaper",
        daemon=True,
    )
    _reaper_thread = thread
    thread.start()


def _reaper_loop() -> None:
    """Background loop: evict idle subscriptions and enforce the global cap."""
    while not _reaper_stop.wait(_REAPER_INTERVAL_SECONDS):
        try:
            _reap_once()
        except Exception:
            logger.exception("blogging event-bus reaper iteration failed")


def _reap_once() -> None:
    """Single reaper pass. Exposed for tests."""
    now = time.monotonic()
    evicted_jobs = 0
    evicted_subs = 0
    woken: list[Subscription] = []

    with _lock:
        # Pass 1: drop subscriptions whose last_activity is older than TTL.
        for job_id in list(_subscribers.keys()):
            subs = _subscribers[job_id]
            kept: list[Subscription] = []
            for sub in subs:
                if now - sub.last_activity > _SUB_TTL_SECONDS:
                    woken.append(sub)
                    evicted_subs += 1
                else:
                    kept.append(sub)
            if kept:
                _subscribers[job_id] = kept
            else:
                del _subscribers[job_id]
                _job_created_at.pop(job_id, None)
                evicted_jobs += 1

        # Pass 2: enforce the global cap by evicting oldest jobs.
        while len(_subscribers) > _MAX_JOBS_TRACKED:
            # _job_created_at is insertion-ordered; the first key is the oldest.
            try:
                oldest_job = next(iter(_job_created_at))
            except StopIteration:
                break
            subs = _subscribers.pop(oldest_job, None) or []
            _job_created_at.pop(oldest_job, None)
            for sub in subs:
                woken.append(sub)
                evicted_subs += 1
            evicted_jobs += 1

    # Wake evicted subscribers OUTSIDE the lock so their consumers can drain
    # and exit without contending.
    for sub in woken:
        sub.notify.set()

    if evicted_jobs or evicted_subs:
        logger.info(
            "blogging event-bus reaper: evicted %d job(s) and %d subscription(s)",
            evicted_jobs,
            evicted_subs,
        )


def subscribe(job_id: str) -> Subscription:
    """Create a subscription for *job_id*. The caller must call :func:`unsubscribe` when done."""
    sub = Subscription()
    with _lock:
        if job_id not in _subscribers:
            _subscribers[job_id] = []
            _job_created_at[job_id] = sub.created_at
        _subscribers[job_id].append(sub)
    _start_reaper_if_needed()
    return sub


def unsubscribe(job_id: str, sub: Subscription) -> None:
    """Remove *sub* from *job_id*'s subscriber list."""
    with _lock:
        subs = _subscribers.get(job_id)
        if subs is not None:
            try:
                subs.remove(sub)
            except ValueError:
                pass
            if not subs:
                del _subscribers[job_id]
                _job_created_at.pop(job_id, None)


def publish(job_id: str, event: Dict[str, Any], *, event_type: Optional[str] = None) -> None:
    """Broadcast *event* to all subscribers of *job_id*.

    Called from pipeline threads — must be thread-safe. If *event_type* is
    given it is merged as ``type`` into the published dict. Refreshes the
    ``last_activity`` timestamp on each matched subscription so active
    streams are not reaped.
    """
    payload: Dict[str, Any] = {}
    if event_type:
        payload["type"] = event_type
    payload["ts"] = datetime.now(timezone.utc).isoformat()
    payload.update(event)

    now = time.monotonic()
    with _lock:
        subs = _subscribers.get(job_id)
        if not subs:
            return
        for sub in subs:
            sub.events.append(payload)
            sub.last_activity = now
            sub.notify.set()


def cleanup_job(job_id: str) -> None:
    """Remove all subscribers for *job_id* (call after terminal event)."""
    with _lock:
        subs = _subscribers.pop(job_id, None)
        _job_created_at.pop(job_id, None)
    if subs:
        for sub in subs:
            sub.notify.set()  # wake any blocked consumers so they exit


def shutdown() -> None:
    """Stop the reaper thread. Call during process shutdown (tests / lifespan)."""
    global _reaper_thread
    _reaper_stop.set()
    thread = _reaper_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    _reaper_thread = None
    _reaper_stop.clear()
