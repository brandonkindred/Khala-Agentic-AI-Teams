"""Shared background-heartbeat driver.

Several teams independently grew the same "background heartbeat" scaffolding: a
``threading.Event`` stop flag, a daemon thread looping on ``event.wait(interval)``,
a best-effort beat, and a ``finally`` that sets the flag and joins. Each copy
drifted slightly. :class:`BackgroundHeartbeat` is the single driver they share.

The driver is deliberately generic — it knows nothing about Temporal or the job
service; it only repeatedly calls a caller-supplied ``beat`` on an interval until
stopped. Lives under ``shared_temporal`` (the suggested home) but importing it
does not pull in the Temporal SDK: every ``temporalio`` import in this package is
function-deferred.

The four axes on which the original copies differed are all parameters here:

- **stop semantics** — externally controlled (``stop()`` / context-manager exit)
  *or* self-terminating via a ``should_continue`` predicate checked each tick.
- **error policy** — silent swallow (default) *or* a caller ``on_error`` hook
  (e.g. ``logger.warning``).
- **context capture** — optionally snapshot ``contextvars.copy_context()`` and run
  the beat inside it (so a Temporal activity handle is visible in the thread).
- **join timeout** — caller-tunable bound on ``stop()``'s join.
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

    Or fire-and-forget with a self-terminating predicate::

        BackgroundHeartbeat(
            lambda: client.heartbeat(job_id),
            120.0,
            should_continue=lambda: job_is_active(job_id),
            on_error=lambda exc: logger.warning("hb %s: %s", job_id, exc),
        ).start()

    Preconditions:
        - ``interval_s`` > 0.
        - ``beat`` is callable and safe to invoke repeatedly from a daemon thread.
        - When ``copy_context`` is True, the constructor runs on the thread whose
          context should be snapshotted (e.g. the Temporal activity thread).

    Postconditions:
        - Between ``start()`` and ``stop()`` exactly one daemon thread is running.
        - The thread exits within one ``interval_s`` of ``stop()`` being called or
          ``should_continue`` returning False.
        - A raising ``beat`` or ``should_continue`` never kills the loop: the
          exception is routed to ``on_error`` (default: swallowed) and the loop
          continues to the next tick.

    Invariants:
        - ``start()`` is idempotent — calling it again while running is a no-op.
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
    ) -> None:
        assert callable(beat), "beat must be callable"
        assert interval_s > 0, "interval_s must be positive"
        self._beat = beat
        self._interval_s = interval_s
        self._name = name
        self._should_continue = should_continue
        self._on_error = on_error
        self._join_timeout = join_timeout
        # Snapshot the *current* context now (constructor runs on the caller's
        # thread) so the beat sees e.g. the Temporal activity handle.
        self._ctx = contextvars.copy_context() if copy_context else None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run_beat(self) -> None:
        if self._ctx is not None:
            self._ctx.run(self._beat)
        else:
            self._beat()

    def _loop(self) -> None:
        # wait() returns True only when the stop flag is set, so a clean timeout
        # (False) drives each beat; the loop exits promptly once stop() is called.
        while not self._stop.wait(self._interval_s):
            try:
                if self._should_continue is not None and not self._should_continue():
                    return
                self._run_beat()
            except BaseException as exc:  # noqa: BLE001 — liveness must survive any beat error
                if self._on_error is not None:
                    self._on_error(exc)

    def start(self) -> "BackgroundHeartbeat":
        """Start the daemon thread (idempotent). Returns self for chaining."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()
        return self

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
