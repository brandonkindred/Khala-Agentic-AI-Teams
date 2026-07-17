"""Shared "coalesce a burst of writes into one background write" primitive.

:class:`LatestValueFlusher` consolidates the "single-slot mailbox + daemon writer
thread" pattern used to move a synchronous, possibly-slow write off a thread that
holds a lock other threads need (see ``coding_team/orchestrator.py``'s task-graph
persist split for the motivating use case). It is stdlib-only and lives in
``shared_concurrency`` alongside :class:`~shared_concurrency.heartbeat.BackgroundHeartbeat`,
whose start/stop/context-manager lifecycle it mirrors.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["LatestValueFlusher"]


class LatestValueFlusher:
    """A daemon thread that writes the most recently enqueued payload via ``writer``.

    Coalesces a burst of ``enqueue()`` calls into a single ``writer`` invocation —
    intended for destinations where a later write overwrites (rather than appends
    to) an earlier one, so only the latest payload as of each drain point needs to
    reach the destination; intermediate payloads superseded before the writer picks
    them up are dropped, not queued.

    Usage as a context manager (preferred — owns start + stop)::

        with LatestValueFlusher(job_client.update_job, name="job-persist") as flusher:
            flusher.enqueue({"status_text": "working"})
            ...
            flusher.drain()  # block until the above (or a fresher payload) has landed

    Fire-and-forget with explicit lifecycle::

        flusher = LatestValueFlusher(write_fn, name="persist").start()
        flusher.enqueue(payload)
        ...
        flusher.stop()

    Preconditions:
        - ``writer`` is callable with exactly one positional argument (the payload)
          and is safe to invoke repeatedly, from a single daemon thread, with no
          concurrent invocation of itself.
        - ``enqueue()`` is only called while the flusher is started and not yet
          stopped (between a ``start()``/``__enter__`` and the matching
          ``stop()``/``__exit__``) — see ``enqueue()``'s own precondition for why.

    Postconditions:
        - Between ``start()`` and ``stop()`` exactly one daemon thread is running.
        - ``enqueue()`` never blocks on the writer and never queues more than one
          payload — a payload not yet picked up by the writer thread is replaced,
          not appended to, by the next ``enqueue()`` call.
        - ``drain()`` does not return (True) until every payload enqueued before the
          call has either been passed to ``writer`` or been superseded by a fresher
          payload that was itself passed to ``writer`` — i.e. the destination
          reflects at least as fresh a state as of the ``drain()`` call once it
          returns True.
        - A raising ``writer`` never kills the loop: the exception (including a
          ``BaseException`` such as ``SystemExit``/``asyncio.CancelledError`` — not
          just ``Exception``, matching ``BackgroundHeartbeat``'s ``_tick``) is
          routed to ``on_error`` (default: logged and swallowed) and the loop
          continues with the next payload. A dead loop would leave every future
          ``drain()``/``stop()`` call blocked forever, since nothing would be left
          to ever mark the mailbox idle again.

    Invariants:
        - ``start()`` is idempotent and safe to call concurrently — calling it
          again (from any thread) while already running is a no-op; at most one
          daemon thread is ever created.
        - At most one payload is ever pending: the mailbox holds zero or one items.
    """

    def __init__(
        self,
        writer: Callable[[Any], None],
        *,
        name: str = "latest-value-flusher",
        on_error: Optional[Callable[[BaseException], None]] = None,
        join_timeout: float = 5.0,
    ) -> None:
        assert callable(writer), "writer must be callable"
        self._writer = writer
        self._name = name
        self._on_error = on_error
        self._join_timeout = join_timeout
        self._lock = threading.Lock()
        self._pending: Any = None
        self._has_pending = False
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Guards the check-create-assign-start sequence in start() (and stop()'s
        # matching teardown) so concurrent callers can never race into creating two
        # daemon threads sharing one mailbox — see the class Invariants.
        self._lifecycle_lock = threading.Lock()

    def start(self) -> "LatestValueFlusher":
        """Start the daemon writer thread (idempotent — a no-op while already running).

        Postconditions:
            - Safe to call concurrently from multiple threads: exactly one daemon
              thread is created and started, no matter how many callers race here.
        """
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_flag.clear()
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
            return self

    def is_alive(self) -> bool:
        """True iff the writer thread has been started and has not yet exited."""
        return self._thread is not None and self._thread.is_alive()

    def enqueue(self, payload: Any) -> None:
        """Replace the pending payload (if any) with ``payload`` and wake the writer.

        Preconditions:
            - The flusher is currently started (``is_alive()``) — enqueuing with no
              live writer thread would clear the idle flag with nothing left able to
              ever set it again, permanently hanging every later ``drain()``/
              ``stop()`` call. This is a caller bug (a race with a concurrent
              ``stop()``, or enqueuing before ``start()``), not something this
              method silently tolerates or coerces around.

        Postconditions:
            - Never blocks on the writer; a payload not yet picked up by the writer
              thread is overwritten, not queued alongside.
        """
        with self._lifecycle_lock:
            assert self.is_alive(), "enqueue() requires a started, not-yet-stopped flusher"
            with self._lock:
                self._pending = payload
                self._has_pending = True
                self._idle.clear()
        self._wake.set()

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Block until there is no pending payload and no write in flight.

        Returns:
            True once idle; False if ``timeout`` elapsed first.
        """
        return self._idle.wait(timeout)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                self._wake.clear()
                payload = self._pending
                has_pending = self._has_pending
                self._pending = None
                self._has_pending = False
            if has_pending:
                try:
                    self._writer(payload)
                except BaseException as exc:  # noqa: BLE001 - the loop (and every future
                    # drain()/stop()) must survive any writer error, including a
                    # BaseException like SystemExit/asyncio.CancelledError — matching
                    # BackgroundHeartbeat._tick's identical rationale: a dead loop would
                    # leave _idle cleared forever, hanging every subsequent caller.
                    if self._on_error is not None:
                        self._on_error(exc)
                    else:
                        logger.warning("LatestValueFlusher %s: writer failed: %s", self._name, exc)
            with self._lock:
                # A fresh enqueue() may have landed while writer() ran above (it does not
                # hold self._lock) — only declare idle/exit when nothing new arrived,
                # otherwise loop back (self._wake is already set) to pick it up.
                if not self._has_pending:
                    self._idle.set()
                    if self._stop_flag.is_set():
                        return

    def stop(self) -> None:
        """Drain any pending payload — waiting as long as it takes — then signal the loop
        to exit and join it.

        The pre-shutdown drain is deliberately unbounded: the whole point of this primitive
        is that the caller depends on the latest enqueued state actually reaching the
        destination, so ``stop()`` must never abandon an outstanding write just because it
        outlasts ``join_timeout`` — a writer whose own client has a longer timeout than
        ``join_timeout`` (e.g. an HTTP call with a multi-second timeout/retry budget) would
        otherwise be left running after the caller considers the flusher stopped, free to
        land a stale write at an arbitrary later time. ``join_timeout`` still bounds the
        final ``Thread.join()`` below, but by then the writer loop has nothing left to do
        but observe the stop flag and exit, so that bound is not load-bearing for delivery.

        Postconditions:
            - Safe to call when never started or already stopped (no-op).
            - Does not return until any payload enqueued before the call has been delivered
              (``writer`` returned) or raised (routed to ``on_error``) — never abandons an
              outstanding write.
            - The thread is then joined for at most ``join_timeout`` seconds.
            - Held under the same lifecycle lock as ``start()``/``enqueue()``'s liveness
              check, so a concurrent ``enqueue()`` either fully precedes this call (and is
              drained by it) or fully follows it (and then correctly fails its precondition
              instead of racing a payload past a dead writer thread).
        """
        with self._lifecycle_lock:
            self.drain()  # unbounded — see docstring
            self._stop_flag.set()
            self._wake.set()
            if self._thread is not None:
                self._thread.join(timeout=self._join_timeout)

    def __enter__(self) -> "LatestValueFlusher":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
