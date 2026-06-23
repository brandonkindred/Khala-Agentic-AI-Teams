"""Shared per-job in-memory event bus for SSE streaming.

Several teams hand-rolled the same process-local pub/sub used to stream a job's
progress to SSE clients: pipeline threads :func:`publish` events; SSE generators
:func:`subscribe` / :func:`unsubscribe` to receive them via a thread-safe
``deque``. This module owns the *algorithm*; each hosting team owns its own
:class:`BusState` (and, optionally, a background reaper), so the buses stay
independent process-local namespaces — there is no shared singleton.

.. warning::
   **Process-local state.** A :class:`BusState` lives in the hosting process for
   its lifetime. Under a multi-worker deployment (``uvicorn --workers N``) or
   multiple replicas, events published on one worker will NOT reach SSE clients
   connected to another. Run single-worker, or front with sticky sessions, until
   migrated to a shared bus (Postgres ``LISTEN/NOTIFY`` or ``agents/event_bus/``).

Optional reaper (:func:`reap_once`): a team that keeps long-lived streams can
bound in-memory growth by periodically reaping subscriptions whose
``last_activity`` is older than a TTL, plus a hard cap on tracked jobs. The TTL
liveness signal is :meth:`Subscription.touch` — a consumer that wants reaping
MUST touch its subscription at least once per TTL while its stream is alive
(publish-side activity alone is not a reliable proxy for a quiet-but-connected
client). Teams that do not call :func:`reap_once` get pure unbounded-until-
cleanup behaviour and need not touch.

Invariants:
    - Every mutation of a :class:`BusState` is performed under ``state.lock``.
    - ``state.subscribers`` and ``state.job_created_at`` share the same key set
      after any operation returns (a job is tracked in both or neither).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "Subscription",
    "BusState",
    "subscribe",
    "unsubscribe",
    "publish",
    "cleanup_job",
    "reap_once",
]


@dataclass
class Subscription:
    """Handle returned by :func:`subscribe`.

    ``created_at`` is fixed at construction; ``last_activity`` is the liveness
    signal the reaper reads. Consumers that rely on reaping should call
    :meth:`touch` each loop iteration while their stream is alive.

    ``events`` is a bounded ``deque(maxlen=500)``: a consumer that falls behind
    by more than 500 undrained events silently loses the **oldest** ones (the
    deque evicts from the left on overflow). SSE consumers should drain promptly
    — a slow reader drops old progress events, never the newest.
    """

    notify: threading.Event = field(default_factory=threading.Event)
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        """Refresh the liveness timestamp (cheap, lock-free — atomic attr write)."""
        self.last_activity = time.monotonic()


@dataclass
class BusState:
    """A team's independent event-bus namespace: lock + subscriber/creation maps.

    Invariants:
        - ``subscribers`` and ``job_created_at`` are only mutated under ``lock``.
        - A job id is present in ``job_created_at`` iff it is present in
          ``subscribers``.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    subscribers: Dict[str, List[Subscription]] = field(default_factory=dict)
    job_created_at: Dict[str, float] = field(default_factory=dict)


def subscribe(state: BusState, job_id: str) -> Subscription:
    """Register and return a new subscription for *job_id*.

    Preconditions:
        - ``job_id`` is a non-empty string; caller will :func:`unsubscribe` when done.
    Postconditions:
        - The returned :class:`Subscription` is appended to ``state.subscribers[job_id]``
          and ``state.job_created_at[job_id]`` records the first subscriber's creation time.
    """
    sub = Subscription()
    with state.lock:
        if job_id not in state.subscribers:
            state.subscribers[job_id] = []
            state.job_created_at[job_id] = sub.created_at
        state.subscribers[job_id].append(sub)
    return sub


def unsubscribe(state: BusState, job_id: str, sub: Subscription) -> None:
    """Remove *sub* from *job_id*'s subscriber list (idempotent).

    Postconditions:
        - ``sub`` is absent from ``state.subscribers[job_id]``; when that list
          empties, the job id is dropped from both maps. Unknown job/sub is a no-op.
    """
    with state.lock:
        subs = state.subscribers.get(job_id)
        if subs is not None:
            try:
                subs.remove(sub)
            except ValueError:
                pass
            if not subs:
                del state.subscribers[job_id]
                state.job_created_at.pop(job_id, None)


def publish(
    state: BusState, job_id: str, event: Dict[str, Any], *, event_type: Optional[str] = None
) -> None:
    """Broadcast *event* to all subscribers of *job_id* (thread-safe).

    Preconditions:
        - ``event`` is a dict; called from pipeline threads.
    Postconditions:
        - A timestamped payload (``ts`` added; ``type`` set when *event_type* is given)
          is appended to every current subscriber's queue and their ``notify`` is set,
          refreshing each subscriber's ``last_activity``. No subscribers ⇒ no-op.
        - The bus's ``ts`` (and ``type`` when *event_type* is given) are authoritative:
          a caller-supplied ``ts``/``type`` in *event* cannot override them.
    """
    # Copy the caller's event first, then stamp the bus's authoritative fields last
    # so a caller-supplied "ts"/"type" can never overwrite them.
    payload: Dict[str, Any] = dict(event)
    if event_type:
        payload["type"] = event_type
    payload["ts"] = datetime.now(timezone.utc).isoformat()

    now = time.monotonic()
    with state.lock:
        subs = state.subscribers.get(job_id)
        if not subs:
            return
        for sub in subs:
            sub.events.append(payload)
            sub.last_activity = now
            sub.notify.set()


def cleanup_job(state: BusState, job_id: str) -> None:
    """Drop and wake every subscriber for *job_id* (call after a terminal event).

    Postconditions:
        - ``job_id`` is absent from both maps; each former subscriber's ``notify``
          is set so blocked consumers exit. Unknown job id is a no-op.
    """
    with state.lock:
        subs = state.subscribers.pop(job_id, None)
        state.job_created_at.pop(job_id, None)
    if subs:
        for sub in subs:
            sub.notify.set()  # wake any blocked consumers so they exit


def reap_once(
    state: BusState,
    *,
    ttl_seconds: float,
    max_jobs: int,
    logger: Optional[Any] = None,
    label: str = "event-bus",
) -> Tuple[int, int]:
    """Single reaper pass: evict idle subscriptions, then enforce the job cap.

    Preconditions:
        - ``ttl_seconds >= 0`` and ``max_jobs >= 0``.
    Postconditions:
        - Subscriptions whose ``last_activity`` is older than ``ttl_seconds`` are
          removed; while more than ``max_jobs`` jobs remain, the oldest (by
          insertion order of ``job_created_at``) are dropped. Every evicted
          subscriber is woken. Returns ``(evicted_jobs, evicted_subs)``.
    """
    if ttl_seconds < 0:
        raise ValueError("ttl_seconds must be >= 0")
    if max_jobs < 0:
        raise ValueError("max_jobs must be >= 0")
    now = time.monotonic()
    evicted_jobs = 0
    evicted_subs = 0
    woken: List[Subscription] = []

    with state.lock:
        # Pass 1: drop subscriptions whose last_activity is older than the TTL.
        for job_id in list(state.subscribers.keys()):
            kept: List[Subscription] = []
            for sub in state.subscribers[job_id]:
                if now - sub.last_activity > ttl_seconds:
                    woken.append(sub)
                    evicted_subs += 1
                else:
                    kept.append(sub)
            if kept:
                state.subscribers[job_id] = kept
            else:
                del state.subscribers[job_id]
                state.job_created_at.pop(job_id, None)
                evicted_jobs += 1

        # Pass 2: enforce the global cap by evicting the oldest jobs.
        while len(state.subscribers) > max_jobs:
            # job_created_at is insertion-ordered; the first key is the oldest.
            try:
                oldest_job = next(iter(state.job_created_at))
            except StopIteration:
                break
            subs = state.subscribers.pop(oldest_job, None) or []
            state.job_created_at.pop(oldest_job, None)
            for sub in subs:
                woken.append(sub)
                evicted_subs += 1
            evicted_jobs += 1

    # Wake evicted subscribers OUTSIDE the lock so consumers drain without contending.
    for sub in woken:
        sub.notify.set()

    if (evicted_jobs or evicted_subs) and logger is not None:
        logger.info(
            "%s reaper: evicted %d job(s) and %d subscription(s)",
            label,
            evicted_jobs,
            evicted_subs,
        )
    return evicted_jobs, evicted_subs
