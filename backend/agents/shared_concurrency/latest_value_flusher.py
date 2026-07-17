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
        - ``enqueue()`` and ``write_now()`` are only called while the flusher is
          started and not yet stopped (between a ``start()``/``__enter__`` and the
          matching ``stop()``/``__exit__``) — see each one's own precondition for
          why.
        - ``drain()``, ``stop()``, and ``write_now()`` are never called from within
          ``writer`` or ``on_error`` (i.e. from the writer thread itself), nor from
          within another ``write_now()`` call's ``fn`` on the same thread (direct
          recursion, or indirectly via ``enqueue()`` followed by ``drain()``) — each
          needs the mailbox to go idle and/or the write lock free, neither of which
          that same thread can make happen until the call already in progress on it
          returns, so a self-call would deadlock. Detected and raised explicitly
          rather than silently hanging.

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
        - ``write_now()`` holds the same serialization point ``writer`` is called
          under for its *entire* call — including flushing any payload already
          sitting in the mailbox — so ``fn`` can never interleave with, or be
          overtaken on the wire by, a payload enqueued concurrently with (or
          racing) the ``write_now()`` call — without blocking ``enqueue()``
          itself, so a concurrent graph/state mutation is never delayed by ``fn``
          being slow.
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

    # Floor for stop()'s final join-poll interval, so a caller-configured join_timeout of
    # (or close to) zero can't turn the loop into a busy-spin — see stop()'s docstring.
    _MIN_JOIN_POLL_INTERVAL = 0.05

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
        # Serializes start()/stop() against each other for their *entire* duration —
        # deliberately separate from _lifecycle_lock, which start()/stop() now hold only
        # briefly to flip _running (see stop()'s docstring for why the long drain/join
        # can't happen under _lifecycle_lock). Without this, a start() racing a stop()
        # that has already flipped _running but not yet confirmed the old thread's exit
        # could begin creating a replacement thread while the old one is still alive —
        # the exact dual-writer race this lock exists to prevent.
        self._shutdown_lock = threading.Lock()
        # The thread currently executing a write_now() call's fn (or self-flush loop), if
        # any — set/cleared only by write_now() itself, always while holding _write_lock, so
        # at most one thread can ever be recorded here at a time. Lets _reject_if_writer_thread
        # detect a REENTRANT drain()/stop()/write_now() call made from within fn — e.g. fn
        # recursively calling write_now(), or enqueuing a payload and then calling drain() —
        # which would otherwise deadlock: _write_lock is not reentrant, and drain()'s _idle
        # wait can only be satisfied by a write this same thread is blocking, unable to make.
        self._write_now_thread: Optional[threading.Thread] = None
        # Admission bookkeeping for write_now(), analogous to enqueue()'s _running check —
        # both read/written only under _lifecycle_lock. _active_write_now_count is the number
        # of write_now() calls admitted (checked _running while True) but not yet finished;
        # _write_now_drained is set iff that count is 0. stop() waits on the event (after
        # flipping _running to False, so the count can only fall from here) to make sure it
        # never returns while an admitted-but-still-queued-for-_write_lock write_now() call
        # could still land a write afterward — see stop()'s docstring.
        self._active_write_now_count = 0
        self._write_now_drained = threading.Event()
        self._write_now_drained.set()

    def _reject_if_writer_thread(self, caller: str) -> None:
        current = threading.current_thread()
        if current is self._thread:
            raise RuntimeError(
                f"{caller}() must not be called from the writer thread itself "
                "(i.e. from within writer or on_error) — it waits for the mailbox to "
                "go idle, which on this thread can only happen after this very call "
                "returns, so it would deadlock"
            )
        if current is self._write_now_thread:
            raise RuntimeError(
                f"{caller}() must not be called from within a write_now() call's fn on "
                "the same thread — that call already holds the (non-reentrant) write "
                "lock write_now()/drain()/stop() all need to make progress, so it would "
                "deadlock"
            )

    def start(self) -> "LatestValueFlusher":
        """Start the daemon writer thread (idempotent — a no-op while already running).

        Postconditions:
            - Safe to call concurrently from multiple threads: exactly one daemon
              thread is created and started, no matter how many callers race here.
            - Serialized against a concurrent stop() end-to-end (via a dedicated
              shutdown lock distinct from the brief one guarding the ``_running``
              flag) — this call cannot begin creating a replacement thread until a
              racing stop() has fully confirmed the old thread's exit. See stop()'s
              docstring for the dual-writer race this prevents.
        """
        with self._shutdown_lock:
            with self._lifecycle_lock:
                if self._running:
                    return self
            self._stop_flag.clear()
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
            with self._lifecycle_lock:
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
        """Run ``fn`` serialized against the flusher's own background writes, holding
        the same lock ``writer`` is called under for this call's *entire* duration —
        including flushing any payload already sitting in the mailbox — so a payload
        enqueued concurrently with (or racing) this call can never reach the wire
        before, or interleaved with, ``fn``. Unlike gating on ``_lifecycle_lock``,
        this never blocks ``enqueue()`` — a concurrent graph/state mutation still
        returns immediately; only the eventual background write of that mutation's
        payload is delayed until ``fn`` finishes.

        Deliberately does not use ``drain()``: draining via the ``_idle`` event and
        then separately acquiring ``_write_lock`` leaves a gap between the two steps
        in which a payload enqueued in between could be claimed and written by the
        background writer thread ahead of ``fn`` — a real ordering violation this
        method exists to prevent, not merely a benign race. Holding ``_write_lock``
        for the whole call closes that gap: the background writer thread also claims
        a payload from the mailbox only while holding ``_write_lock`` (see ``_run``),
        so while this call holds it, the writer thread can neither claim nor write a
        newly enqueued payload — every payload that races this call is therefore
        written either by this call itself (before ``fn``, via the self-flush loop
        below) or by the background writer (necessarily after ``fn``, once this call
        returns), never interleaved with or ahead of it. A version of this method that
        let the writer thread claim a payload without ``_write_lock`` would reopen the
        same gap one level deeper: this call could see an empty mailbox (nothing left
        in ``_has_pending``) while the writer thread was still holding an
        already-claimed-but-not-yet-written payload, and run ``fn`` before that write
        landed.

        Preconditions:
            - Not called from the writer thread itself, nor from within another
              ``write_now()`` call's ``fn`` on the same thread — see the class
              Preconditions.
            - The flusher is currently started and not yet stopped — same
              liveness requirement as ``enqueue()``, and for the same reason:
              ``stop()`` must be able to know, by the time it starts waiting, the
              complete set of ``write_now()`` calls it needs to wait for.

        Postconditions:
            - Any payload that was pending (enqueued but not yet written) at the
              start of this call has been flushed before ``fn`` runs.
            - ``fn`` has returned (or raised — propagated to the caller, not
              swallowed; unlike a background write, a caller-driven ``write_now()``
              failure is the caller's to handle) before this call returns.
        """
        self._reject_if_writer_thread("write_now")
        with self._lifecycle_lock:
            if not self._running:
                raise RuntimeError("write_now() requires a started, not-yet-stopped flusher")
            self._active_write_now_count += 1
            self._write_now_drained.clear()
        try:
            with self._write_lock:
                # Recorded only while holding _write_lock, so _reject_if_writer_thread can
                # never observe a stale value from a call that already released the lock.
                self._write_now_thread = threading.current_thread()
                try:
                    while True:
                        with self._lock:
                            if not self._has_pending:
                                break
                            payload = self._pending
                            self._pending = None
                            self._has_pending = False
                        try:
                            self._writer(payload)
                        except BaseException as exc:  # noqa: BLE001 - see _run()'s rationale
                            self._report_error(exc)
                        with self._lock:
                            if not self._has_pending:
                                self._idle.set()
                    fn()
                finally:
                    self._write_now_thread = None
        finally:
            with self._lifecycle_lock:
                self._active_write_now_count -= 1
                if self._active_write_now_count == 0:
                    self._write_now_drained.set()

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                self._wake.clear()
            # Claiming (removing the payload from the mailbox) must happen under
            # _write_lock, atomically with the write itself — not before it. If this
            # thread claimed the payload first and only then queued up for _write_lock,
            # write_now() could acquire that lock in between, observe an empty mailbox
            # (nothing left in _has_pending, even though this thread is holding an
            # already-claimed payload it hasn't written yet), and run its fn() first —
            # letting this thread's write land AFTER write_now()'s fn even though it was
            # claimed (and thus logically committed to being written) before write_now()
            # ever checked. Holding _write_lock across both the claim and the write closes
            # that gap: write_now() can only ever observe an empty mailbox once every
            # payload claimed-before-it-checked has also already been written.
            with self._write_lock:
                with self._lock:
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

        The pre-shutdown ``drain()`` only tracks background mailbox writes, so it can return
        while a caller-driven ``write_now()`` call is still in flight — ``stop()`` additionally
        waits for ``_write_now_drained``, or a direct write could land on the wire after the
        caller believes shutdown has completed. A single acquire-then-release of ``_write_lock``
        is not enough here: a ``write_now()`` call can already be *queued* waiting for that lock
        (not holding it yet) when ``stop()`` reaches this point, and a bare acquire/release only
        proves the lock was free at some instant — it does not wait for that queued caller to
        actually get its turn and run ``fn``. ``_write_now_drained`` instead reflects
        ``_active_write_now_count``, incremented/decremented by every ``write_now()`` call
        across its *entire* duration (queued-and-waiting included, not just while holding
        ``_write_lock``) — so waiting on it covers every ``write_now()`` call admitted before
        this point, however far into acquiring the lock it had gotten. ``write_now()`` is
        admitted under the same ``_running`` check ``enqueue()`` uses, so once this call flips
        ``_running`` to False (below), no *new* ``write_now()`` call can be admitted — the count
        this call waits for can only fall from here, never rise.

        This call flips ``_running`` under ``_lifecycle_lock`` (briefly — the same lock
        ``enqueue()``'s liveness check uses) but does *not* hold that lock across the drain
        and join below: a ``writer``/``on_error`` callback running on the daemon thread is
        permitted to call ``enqueue()`` on itself (see the class Preconditions — only
        ``drain()``/``stop()``/``write_now()`` are forbidden there), and holding
        ``_lifecycle_lock`` across the drain would deadlock that self-enqueue against this
        call: it would block acquiring ``_lifecycle_lock`` while this call blocks waiting for
        ``_idle``, which can't be set until the callback returns. Since ``_running`` is
        already False by the time the drain begins, such a self-enqueue now fails fast with
        ``RuntimeError`` instead (its documented precondition) rather than deadlocking either
        side. A concurrent ``start()``/second ``stop()`` is instead serialized against this
        call end-to-end via ``_shutdown_lock``, held for the whole method.

        The final join loop polls at ``join_timeout`` (or a small minimum, whichever is
        larger) rather than the raw configured value: a ``join_timeout`` of (or close to)
        zero — an accepted configuration, used by several tests to make other races easier to
        hit — would otherwise turn the loop into a busy-spin that can consume a full core and
        flood the log with an identical warning on every iteration while the daemon thread
        finishes exiting. The warning itself is logged only once per call, not on every poll.

        Postconditions:
            - Safe to call when never started or already stopped (no-op).
            - Does not return until any payload enqueued before the call has been delivered
              (``writer`` returned) or raised (routed to ``on_error``) — never abandons an
              outstanding write.
            - Does not return until every ``write_now()`` call admitted before this call —
              whether already holding ``_write_lock`` or still queued waiting for it — has
              completed. Never returns while a direct write is still in flight or pending.
            - Does not return until the writer thread has actually terminated — never leaves
              a live thread behind for a subsequent ``start()`` to race against.
            - Serialized against a concurrent ``start()``/``stop()`` end-to-end via a
              dedicated shutdown lock, so a racing ``start()`` cannot begin creating a
              replacement thread until this call has confirmed the old thread is fully gone.

        Preconditions:
            - Not called from the writer thread itself — see the class Preconditions.
        """
        self._reject_if_writer_thread("stop")
        with self._shutdown_lock:
            with self._lifecycle_lock:
                if not self._running:
                    return
                self._running = False
            self.drain()  # unbounded — see docstring; not held under _lifecycle_lock
            self._write_now_drained.wait()  # unbounded — waits out every admitted write_now()
            self._stop_flag.set()
            self._wake.set()
            poll_timeout = max(self._join_timeout, self._MIN_JOIN_POLL_INTERVAL)
            warned = False
            while self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=poll_timeout)
                if self._thread.is_alive() and not warned:
                    warned = True
                    logger.warning(
                        "LatestValueFlusher %s: writer thread still exiting after %.1fs",
                        self._name,
                        self._join_timeout,
                    )

    def __enter__(self) -> "LatestValueFlusher":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
