"""Shared background-heartbeat / interval-loop driver.

:class:`BackgroundHeartbeat` consolidates the daemon-thread + ``event.wait(interval)``
loop + best-effort-beat + stop/join scaffolding that several teams had each
hand-rolled. It is Temporal-agnostic and lives in ``shared_concurrency`` (stdlib
only) so non-Temporal callers can use it without importing ``shared_temporal``. See
the class docstring for the parameters and ``shared_concurrency/README.md`` for the
rationale and usage examples.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["BackgroundHeartbeat"]


class BackgroundHeartbeat:
    """A daemon thread that calls ``beat`` every ``interval_s`` until stopped.

    Usage as a context manager (preferred — owns start + stop)::

        with BackgroundHeartbeat(activity.heartbeat, 30.0, copy_context=True):
            do_long_blocking_work()

    Fire-and-forget with a self-terminating predicate::

        BackgroundHeartbeat(
            lambda: client.heartbeat(job_id),
            120.0,
            should_continue=lambda: job_is_active(job_id),
            on_error=lambda exc: logger.warning("hb %s: %s", job_id, exc),
        ).start()

    Fire-and-forget with a caller-held stop handle (beats immediately on start)::

        stop = threading.Event()
        BackgroundHeartbeat(sweep, 60.0, beat_first=True, stop_event=stop).start()
        ...  # later: stop.set()

    Preconditions:
        - ``interval_s`` > 0.
        - ``beat`` is callable and safe to invoke repeatedly from a daemon thread.
        - When ``copy_context`` is True, the constructor runs on the thread whose
          context should be snapshotted (e.g. the Temporal activity thread).

    Postconditions:
        - Between ``start()`` and ``stop()`` exactly one daemon thread is running.
        - The thread exits within one ``interval_s`` of the stop flag being set or
          ``should_continue`` returning False.
        - A raising ``beat`` or ``should_continue`` never kills the loop: the
          exception is routed to ``on_error`` (default: swallowed) and the loop
          continues to the next tick.

    Invariants:
        - ``start()`` is idempotent — calling it again while running is a no-op.
        - ``beat`` and ``should_continue`` always run in the same context (both
          inside the captured context when ``copy_context`` is set, else both bare).
    """

    def __init__(
        self,
        beat: Callable[[], None],
        interval_s: float,
        *,
        name: str = "background-heartbeat",
        should_continue: Optional[Callable[[], bool]] = None,
        copy_context: bool = False,
        on_error: Optional[Callable[[BaseException], None]] = None,
        join_timeout: float = 5.0,
        beat_first: bool = False,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        assert callable(beat), "beat must be callable"
        assert interval_s > 0, "interval_s must be positive"
        self._beat = beat
        self._interval_s = interval_s
        self._name = name
        self._should_continue = should_continue
        self._on_error = on_error
        self._join_timeout = join_timeout
        self._beat_first = beat_first
        # Snapshot the *current* context now (constructor runs on the caller's
        # thread) so the beat sees e.g. the Temporal activity handle.
        self._ctx = contextvars.copy_context() if copy_context else None
        # An injected stop event lets a caller keep a raw handle; otherwise the
        # driver owns a fresh (already-clear) event. The event is never re-cleared,
        # so a pre-set injected event stops the loop immediately.
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run_in_ctx(self, fn: Callable[[], object]) -> object:
        return self._ctx.run(fn) if self._ctx is not None else fn()

    def _tick(self) -> bool:
        """Run one predicate-check + beat. Returns False when the loop should stop."""
        try:
            if self._should_continue is not None and not self._run_in_ctx(self._should_continue):
                return False
            self._run_in_ctx(self._beat)
        except BaseException as exc:  # noqa: BLE001 — liveness must survive any beat error
            if self._on_error is not None:
                self._on_error(exc)
        return True

    def _loop(self) -> None:
        # wait() returns True only when the stop flag is set, so a clean timeout
        # (False) drives each beat; the loop exits promptly once stop is signalled.
        if self._beat_first and not self._stop.is_set():
            if not self._tick():
                return
        while not self._stop.wait(self._interval_s):
            if not self._tick():
                return

    def start(self) -> "BackgroundHeartbeat":
        """Start the daemon thread (idempotent — a no-op while already running)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()
        return self

    def is_alive(self) -> bool:
        """True iff the beater thread has been started and has not yet exited."""
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Signal the loop to exit and join the thread (bounded by ``join_timeout``).

        Postconditions:
            - The stop flag is set; the thread is joined for at most
              ``join_timeout`` seconds. Safe to call when never started or already
              stopped (no-op).
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._join_timeout)

    def __enter__(self) -> "BackgroundHeartbeat":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
