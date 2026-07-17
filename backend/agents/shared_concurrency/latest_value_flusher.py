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
        - ``drain()``, ``stop()``, and ``write_now()`` are never called from within
          ``writer`` or ``on_error`` (i.e. from the writer thread itself) — each
          waits for the mailbox to go idle, which on that thread can only happen
          after the very call in progress returns, so a self-call would deadlock.
          Detected and raised explicitly rather than silently hanging.

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
        - ``write_now()`` calls ``writer`` and a caller-supplied ``fn`` on the same
          serialization point, so ``fn`` can never interleave with, or be overtaken
          on the wire by, a payload enqueued concurrently with (or racing) the
          ``write_now()`` call — without blocking ``enqueue()`` itself, so a
          concurrent graph/state mutation is never delayed by ``fn`` being slow.
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
        # Explicit logical lifecycle state, distinct from thread.is_alive(): a bounded
        # Thread.join() in stop() can time out and return before the OS thread has
        # actually finished exiting, so is_alive() can still read True for a brief
        # window after stop() has committed to shutting down. Only ever read/written
        # under _lifecycle_lock, so start()/enqueue() always see the state stop()
        # (or start()) last committed, never a stale thread-scheduling artifact.
        self._running = False
        # Serializes writer(...) calls — both the flusher's own background writes and
        # a caller's write_now(fn) — so the two can never interleave and a payload
        # enqueued concurrently with a write_now() call can never reach the wire before
        # (or during) it. Deliberately separate from _lifecycle_lock: enqueue() never
        # touches this lock, so a concurrent graph/state mutation is never blocked
        # waiting for a slow write_now() call — only the resulting BACKGROUND WRITE of
        # that mutation's payload is delayed until write_now()'s fn releases the lock.
        self._write_lock = threading.Lock()

    def _reject_if_writer_thread(self, caller: str) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                f"{caller}() must not be called from the writer thread itself "
                "(i.e. from within writer or on_error) — it waits for the mailbox to "
                "go idle, which on this thread can only happen after this very call "
                "returns, so it would deadlock"
            )

    def start(self) -> "LatestValueFlusher":
        """Start the daemon writer thread (idempotent — a no-op while already running).

        Postconditions:
            - Safe to call concurrently from multiple threads: exactly one daemon
              thread is created and started, no matter how many callers race here.
        """
        with self._lifecycle_lock:
            if self._running:
                return self
            self._stop_flag.clear()
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
            self._running = True
            return self

    def is_alive(self) -> bool:
        """True iff the writer thread has been started and has not yet exited."""
        return self._thread is not None and self._thread.is_alive()

    def enqueue(self, payload: Any) -> None:
        """Replace the pending payload (if any) with ``payload`` and wake the writer.

        Preconditions:
            - The flusher is currently started and not yet stopped — enqueuing with
              no live writer thread would clear the idle flag with nothing left able
              to ever set it again, permanently hanging every later ``drain()``/
              ``stop()`` call. This is a caller bug (a race with a concurrent
              ``stop()``, or enqueuing before ``start()``), not something this
              method silently tolerates or coerces around. Enforced with an explicit
              raise rather than ``assert``: ``-O``/``PYTHONOPTIMIZE`` strips asserts,
              and silently skipping this specific check reintroduces the permanent
              hang it exists to prevent — this is a liveness invariant, not a
              debug-only sanity check.

        Postconditions:
            - Never blocks on the writer; a payload not yet picked up by the writer
              thread is overwritten, not queued alongside.
        """
        with self._lifecycle_lock:
            if not self._running:
                raise RuntimeError("enqueue() requires a started, not-yet-stopped flusher")
            with self._lock:
                self._pending = payload
                self._has_pending = True
                self._idle.clear()
        self._wake.set()

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Block until there is no pending payload and no write in flight.

        Preconditions:
            - Not called from the writer thread itself — see the class Preconditions.

        Returns:
            True once idle; False if ``timeout`` elapsed first.
        """
        self._reject_if_writer_thread("drain")
        return self._idle.wait(timeout)

    def write_now(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` serialized against the flusher's own background writes: drains
        first, then holds the same lock ``writer`` is called under while running
        ``fn`` — so a payload enqueued concurrently with (or racing) this call can
        never reach the wire before, or interleaved with, ``fn``. Unlike gating on
        ``_lifecycle_lock``, this never blocks ``enqueue()`` — a concurrent
        graph/state mutation still returns immediately; only the eventual background
        write of that mutation's payload is delayed until ``fn`` finishes.

        Preconditions:
            - Not called from the writer thread itself — see the class Preconditions.

        Postconditions:
            - ``fn`` has returned (or raised — propagated to the caller, not
              swallowed; unlike a background write, a caller-driven ``write_now()``
              failure is the caller's to handle) before this call returns.
        """
        self._reject_if_writer_thread("write_now")
        self.drain()
        with self._write_lock:
            fn()

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
                    with self._write_lock:
                        self._writer(payload)
                except BaseException as exc:  # noqa: BLE001 - the loop (and every future
                    # drain()/stop()) must survive any writer error, including a
                    # BaseException like SystemExit/asyncio.CancelledError — matching
                    # BackgroundHeartbeat._tick's identical rationale: a dead loop would
                    # leave _idle cleared forever, hanging every subsequent caller.
                    self._report_error(exc)
            with self._lock:
                # A fresh enqueue() may have landed while writer() ran above (it does not
                # hold self._lock) — only declare idle/exit when nothing new arrived,
                # otherwise loop back (self._wake is already set) to pick it up.
                if not self._has_pending:
                    self._idle.set()
                    if self._stop_flag.is_set():
                        return

    def _report_error(self, exc: BaseException) -> None:
        """Route a writer failure to ``on_error``, isolated from the rest of ``_run``.

        A broken ``on_error`` callback raising in turn must not be able to kill the
        writer loop either — the same "liveness first" requirement ``_run`` applies to
        the writer itself applies one level deeper here, since an exception escaping
        this method would propagate out of ``_run`` before the idle-flag bookkeeping
        runs, permanently hanging every later ``drain()``/``stop()`` call exactly like
        an unguarded writer failure would.
        """
        try:
            if self._on_error is not None:
                self._on_error(exc)
            else:
                logger.warning("LatestValueFlusher %s: writer failed: %s", self._name, exc)
        except BaseException:  # noqa: BLE001 - on_error itself must never kill the loop
            logger.exception("LatestValueFlusher %s: on_error callback raised", self._name)

    def stop(self) -> None:
        """Drain any pending payload — waiting as long as it takes — then signal the loop
        to exit and join it, waiting until it has actually terminated.

        The pre-shutdown drain is deliberately unbounded: the whole point of this primitive
        is that the caller depends on the latest enqueued state actually reaching the
        destination, so ``stop()`` must never abandon an outstanding write just because it
        outlasts ``join_timeout`` — a writer whose own client has a longer timeout than
        ``join_timeout`` (e.g. an HTTP call with a multi-second timeout/retry budget) would
        otherwise be left running after the caller considers the flusher stopped, free to
        land a stale write at an arbitrary later time.

        The final join is equally load-bearing, for a sharper reason than "clean shutdown":
        a bounded ``join(timeout=join_timeout)`` can return while the OS thread is still
        finishing its exit, and if ``start()`` were then called immediately, it would clear
        the shared ``_stop_flag`` before the old thread reaches its own check of that flag —
        so the old thread would loop back to wait for more work instead of exiting, and two
        writer threads would now be alive, both able to pull from the single-slot mailbox and
        call ``writer`` concurrently. That breaks the one-writer-at-a-time guarantee the whole
        "latest value wins" design depends on and can reorder writes on the wire. So ``stop()``
        loops on ``join(timeout=join_timeout)`` — a polling granularity, not a hard cutoff —
        until ``is_alive()`` is actually False, logging a warning each time it doesn't exit
        promptly (this should be at most theoretical: the writer has nothing left to do after
        an already-unbounded ``drain()`` but observe a flag and return).

        Postconditions:
            - Safe to call when never started or already stopped (no-op).
            - Does not return until any payload enqueued before the call has been delivered
              (``writer`` returned) or raised (routed to ``on_error``) — never abandons an
              outstanding write.
            - Does not return until the writer thread has actually terminated — never leaves
              a live thread behind for a subsequent ``start()`` to race against.
            - Held under the same lifecycle lock as ``start()``/``enqueue()``'s liveness
              check, so a concurrent ``enqueue()`` either fully precedes this call (and is
              drained by it) or fully follows it (and then correctly fails its precondition
              instead of racing a payload past a dead writer thread), and a concurrent
              ``start()`` cannot begin creating a replacement thread until this one has
              confirmed the old thread is fully gone.

        Preconditions:
            - Not called from the writer thread itself — see the class Preconditions.
        """
        self._reject_if_writer_thread("stop")
        with self._lifecycle_lock:
            if not self._running:
                return
            self._running = False
            self.drain()  # unbounded — see docstring
            self._stop_flag.set()
            self._wake.set()
            while self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=self._join_timeout)
                if self._thread.is_alive():
                    logger.warning(
                        "LatestValueFlusher %s: writer thread still exiting after %.1fs",
                        self._name,
                        self._join_timeout,
                    )

    def __enter__(self) -> "LatestValueFlusher":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
