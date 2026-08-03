"""Reusable reaper-equipped binding for a :class:`~shared.job_event_bus.bus.BusState`.

Several teams stream a job's progress over SSE and keep long-lived connections
open. To bound in-memory growth under abnormal conditions (a crash that skips
:func:`cleanup_job`, or an SSE client that abandons its connection without its
``finally`` running), a team can run a background reaper that periodically evicts
idle subscriptions past a TTL and enforces a hard cap on tracked jobs.

That reaper plumbing — a lazily-started daemon thread, an idempotent
check-and-start under the bus lock, and a deadlock-free shutdown — was
hand-rolled per team. :class:`ReaperHandle` owns it once so a team gets the
reaper by constructing one handle over its :class:`BusState` instead of
re-implementing the thread lifecycle.

**Consumers MUST call** :meth:`Subscription.touch` at least once per TTL while
their stream is alive: the reaper uses ``last_activity`` as its liveness signal,
and publish-side activity is not a reliable proxy — a legitimately connected
consumer can go quiet for long stretches (a keepalive window, or a job waiting on
human input). Evicting an actively connected consumer would drop its later
terminal event, so the contract is: if you're still reading, touch.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from shared.concurrency import BackgroundHeartbeat
from shared.job_event_bus.bus import BusState, FloatSource, IntSource, reap_once, resolve_float, resolve_int

_DEFAULT_LOGGER = logging.getLogger(__name__)

__all__ = ["ReaperHandle"]


class ReaperHandle:
    """Owns a single lazily-started background reaper for one :class:`BusState`.

    Preconditions:
        - ``state`` is the :class:`BusState` whose subscriptions this reaper bounds.
        - ``interval_seconds > 0`` (the wake-up cadence of the reaper thread).
        - ``ttl_seconds`` / ``max_jobs`` are non-negative values, or zero-arg
          callables returning non-negative values; both are resolved live on each
          pass, so retuning the source affects the next reap.

    Postconditions:
        - :meth:`ensure_started` leaves exactly one reaper thread running; until
          it is called no thread exists (lazy).
        - :meth:`shutdown` stops the thread and leaves the handle re-startable.

    Invariants:
        - At most one reaper thread runs at a time. The check-and-start in
          :meth:`ensure_started` is performed under ``state.lock``, so a burst of
          concurrent callers cannot orphan a second beater.
    """

    def __init__(
        self,
        state: BusState,
        *,
        ttl_seconds: FloatSource,
        max_jobs: IntSource,
        interval_seconds: float,
        name: str,
        label: str = "event-bus",
        logger: Optional[Any] = None,
    ) -> None:
        assert interval_seconds > 0, "interval_seconds must be positive"
        self._state = state
        self._ttl_seconds = ttl_seconds
        self._max_jobs = max_jobs
        self._interval_seconds = float(interval_seconds)
        self._name = name
        self._label = label
        self._logger = logger if logger is not None else _DEFAULT_LOGGER
        self._reaper: Optional[BackgroundHeartbeat] = None

    def ensure_started(self) -> None:
        """Lazily start the reaper; idempotent and concurrency-safe.

        The check-and-start runs under ``state.lock`` so concurrent callers can't
        double-start and orphan a beater. Spawning the thread under the lock is
        safe — it does no join, and the new beater's first :meth:`reap_once` is a
        full interval away.
        """
        with self._state.lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = BackgroundHeartbeat(
                self.reap_once,
                self._interval_seconds,
                name=self._name,
                join_timeout=2.0,
                on_error=lambda exc: self._logger.error(
                    "%s reaper iteration failed", self._label, exc_info=exc
                ),
            ).start()

    def reap_once(self) -> Tuple[int, int]:
        """Single reaper pass (exposed for tests). Resolves the TTL/cap live.

        Preconditions:
            - The resolved ``ttl_seconds``/``max_jobs`` are non-negative (enforced
              by the underlying :func:`~shared.job_event_bus.bus.reap_once`, which
              raises ``ValueError`` otherwise).
        Postconditions:
            - Subscriptions idle past the TTL and the oldest jobs over the cap are
              detached, marked ``closed``, and woken. Returns
              ``(evicted_jobs, evicted_subs)``.
        """
        return reap_once(
            self._state,
            ttl_seconds=resolve_float(self._ttl_seconds),
            max_jobs=resolve_int(self._max_jobs),
            logger=self._logger,
            label=self._label,
        )

    def shutdown(self) -> None:
        """Stop the reaper thread; idempotent and re-startable.

        The handle is cleared under ``state.lock``, but ``stop()`` (which joins the
        beater whose :meth:`reap_once` also takes ``state.lock``) runs OUTSIDE the
        lock to avoid a deadlock.
        """
        with self._state.lock:
            reaper = self._reaper
            self._reaper = None
        if reaper is not None:
            reaper.stop()
