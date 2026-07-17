"""Unit tests for the shared :class:`LatestValueFlusher` mailbox/writer driver.

Covers the coalescing contract (a burst of enqueues collapses to the latest
payload, never queues N), drain()'s ordering guarantee, error routing (swallow
vs. on_error), and the start/stop/context-manager lifecycle shared with
``BackgroundHeartbeat``.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from shared_concurrency.latest_value_flusher import LatestValueFlusher


def test_enqueue_then_drain_delivers_the_payload() -> None:
    """A single enqueue()'d payload reaches the writer by the time drain() returns."""
    written: list[int] = []
    flusher = LatestValueFlusher(written.append, name="single").start()
    flusher.enqueue(42)
    assert flusher.drain(timeout=5) is True
    assert written == [42]
    flusher.stop()


def test_drain_with_nothing_pending_returns_immediately() -> None:
    """drain() on an idle flusher (nothing ever enqueued) returns True without waiting."""
    flusher = LatestValueFlusher(lambda payload: None, name="idle").start()
    start = time.monotonic()
    assert flusher.drain(timeout=5) is True
    assert time.monotonic() - start < 1.0, "drain() must not block when already idle"
    flusher.stop()


def test_burst_of_enqueues_coalesces_to_fewer_writer_calls() -> None:
    """N enqueues faster than the writer can drain collapse into far fewer writes, and the
    last write observed is always the most recently enqueued payload — never a stale one."""
    release = threading.Event()
    calls: list[int] = []

    def slow_writer(payload: int) -> None:
        release.wait(timeout=5)  # hold the first write open while the burst piles up
        calls.append(payload)

    flusher = LatestValueFlusher(slow_writer, name="burst").start()
    flusher.enqueue(0)
    time.sleep(0.02)  # let the writer thread pick up payload 0 and block in slow_writer
    for i in range(1, 21):
        flusher.enqueue(i)
    release.set()
    assert flusher.drain(timeout=5) is True
    flusher.stop()

    assert calls[-1] == 20, "the final write must reflect the latest enqueued payload"
    assert len(calls) < 21, "a burst of 21 enqueues must not result in 21 separate writes"


def test_slow_writer_with_concurrent_enqueue_does_not_lose_the_final_payload() -> None:
    """A payload enqueued while the writer is mid-write is not dropped — drain() still waits
    for it to land (directly exercises the perf fix's durability requirement)."""
    calls: list[str] = []
    started = threading.Event()

    def writer(payload: str) -> None:
        started.set()
        time.sleep(0.05)
        calls.append(payload)

    flusher = LatestValueFlusher(writer, name="concurrent").start()
    flusher.enqueue("first")
    started.wait(timeout=5)  # writer is now blocked in time.sleep on "first"
    flusher.enqueue("final")
    assert flusher.drain(timeout=5) is True
    flusher.stop()

    assert calls[-1] == "final", "a payload enqueued mid-write must still be flushed"


def test_enqueue_never_blocks_on_a_slow_writer() -> None:
    """enqueue() returns immediately even while the writer thread is stuck mid-write."""
    release = threading.Event()
    flusher = LatestValueFlusher(lambda payload: release.wait(timeout=5), name="nonblock").start()
    flusher.enqueue(1)
    time.sleep(0.02)
    start = time.monotonic()
    flusher.enqueue(2)  # writer is still blocked on payload 1's write
    assert time.monotonic() - start < 0.5, "enqueue() must never block on the writer"
    release.set()
    flusher.drain(timeout=5)
    flusher.stop()


def test_raising_writer_routes_to_on_error_and_loop_survives() -> None:
    """A writer exception is delivered to on_error, and the next payload still flushes."""
    errors: list[BaseException] = []
    written: list[int] = []

    def flaky_writer(payload: int) -> None:
        if payload == 1:
            raise RuntimeError("write failed")
        written.append(payload)

    flusher = LatestValueFlusher(flaky_writer, name="flaky", on_error=errors.append).start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True
    flusher.enqueue(2)
    assert flusher.drain(timeout=5) is True
    flusher.stop()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert written == [2], "the loop must survive a writer exception and process later payloads"


def test_writer_error_without_on_error_is_swallowed() -> None:
    """No on_error supplied: a raising writer is logged and swallowed, never kills the loop."""
    written: list[int] = []

    def flaky_writer(payload: int) -> None:
        if payload == 1:
            raise RuntimeError("boom")
        written.append(payload)

    flusher = LatestValueFlusher(flaky_writer, name="swallow").start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True
    assert flusher.is_alive(), "the writer thread must survive an unrouted exception"
    flusher.enqueue(2)
    assert flusher.drain(timeout=5) is True
    flusher.stop()

    assert written == [2]


def test_stop_flushes_a_payload_enqueued_just_before_it() -> None:
    """stop() drains before shutting down, so a payload enqueued right before it still lands."""
    written: list[int] = []
    flusher = LatestValueFlusher(written.append, name="stop-flush").start()
    flusher.enqueue(99)
    flusher.stop()
    assert written == [99]


def test_stop_waits_for_a_writer_slower_than_join_timeout() -> None:
    """stop() must not abandon an outstanding write just because it outlasts join_timeout —
    a writer whose own client has a longer timeout (e.g. an HTTP call) must still be allowed
    to finish, or a stale write could land after the caller considers the flusher stopped."""
    write_delay = 0.2
    written: list[int] = []

    def slow_writer(payload: int) -> None:
        time.sleep(write_delay)
        written.append(payload)

    # join_timeout far shorter than the write itself: a bounded pre-stop drain would return
    # (and stop() would proceed to signal shutdown) while the write is still in flight.
    flusher = LatestValueFlusher(slow_writer, name="slow-stop", join_timeout=0.01).start()
    flusher.enqueue(1)
    start = time.monotonic()
    flusher.stop()
    elapsed = time.monotonic() - start

    assert elapsed >= write_delay, "stop() must wait for the in-flight write, not just join_timeout"
    assert written == [1], "the write must complete before stop() returns"


def test_stop_joins_thread_promptly() -> None:
    """stop() joins the writer thread; it is no longer alive afterwards."""
    flusher = LatestValueFlusher(lambda payload: None, name="join-test").start()
    flusher.enqueue(1)
    flusher.stop()
    assert not flusher.is_alive()
    assert not [t for t in threading.enumerate() if t.name == "join-test"]


def test_stop_before_start_is_safe() -> None:
    """stop() on a never-started flusher is a no-op (no thread to join)."""
    flusher = LatestValueFlusher(lambda payload: None)
    flusher.stop()  # must not raise
    assert not flusher.is_alive()


def test_start_is_idempotent() -> None:
    """Calling start() twice does not spawn a second thread for the same flusher."""
    flusher = LatestValueFlusher(lambda payload: None, name="idem").start()
    flusher.start()  # no-op while alive
    named = [t for t in threading.enumerate() if t.name == "idem"]
    assert len(named) == 1, f"expected one thread, found {len(named)}"
    flusher.stop()


def test_context_manager_starts_and_stops() -> None:
    """The context-manager form starts on enter, drains, and joins on exit."""
    written: list[int] = []
    with LatestValueFlusher(written.append, name="ctx") as flusher:
        assert flusher.is_alive()
        flusher.enqueue(7)
    assert written == [7]
    assert not [t for t in threading.enumerate() if t.name == "ctx"], "thread must join on exit"


def test_invalid_writer_rejected() -> None:
    """Precondition: a non-callable writer is rejected."""
    with pytest.raises(AssertionError):
        LatestValueFlusher(None)  # type: ignore[arg-type]


def test_writer_raising_base_exception_does_not_hang_the_loop() -> None:
    """A writer raising a BaseException that is not an Exception (e.g. the shape of
    SystemExit/asyncio.CancelledError) must not kill the daemon thread — otherwise _idle
    is never set again and every later drain()/stop() call hangs forever."""
    errors: list[BaseException] = []
    written: list[int] = []

    def flaky_writer(payload: int) -> None:
        if payload == 1:
            raise SystemExit("simulated non-Exception failure")
        written.append(payload)

    flusher = LatestValueFlusher(flaky_writer, name="base-exc", on_error=errors.append).start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True, "drain() must not hang after a BaseException"
    assert flusher.is_alive(), "the writer thread must survive a BaseException"
    assert len(errors) == 1
    assert isinstance(errors[0], SystemExit)

    flusher.enqueue(2)
    assert flusher.drain(timeout=5) is True
    flusher.stop()
    assert written == [2]


def test_enqueue_before_start_raises_instead_of_hanging() -> None:
    """enqueue() on a never-started flusher must fail fast (precondition violation) rather
    than clear _idle with no thread ever able to set it back — which would hang every
    later drain()/stop() call forever. Raises RuntimeError (not assert) since this check
    must survive -O/PYTHONOPTIMIZE, where asserts are stripped."""
    flusher = LatestValueFlusher(lambda payload: None, name="not-started")
    with pytest.raises(RuntimeError):
        flusher.enqueue(1)
    assert flusher.drain(timeout=1) is True, "idle flag must be untouched by the rejected enqueue"


def test_enqueue_after_stop_raises_instead_of_hanging() -> None:
    """enqueue() after stop() must also fail fast, not silently hang a later drain()."""
    flusher = LatestValueFlusher(lambda payload: None, name="post-stop").start()
    flusher.stop()
    with pytest.raises(RuntimeError):
        flusher.enqueue(1)


def test_concurrent_start_creates_exactly_one_thread() -> None:
    """N threads calling start() concurrently on the same flusher must create and run
    exactly one daemon thread, never two racing on the same single-slot mailbox."""
    flusher = LatestValueFlusher(lambda payload: None, name="concurrent-start")
    barrier = threading.Barrier(10)

    def _start() -> None:
        barrier.wait(timeout=5)
        flusher.start()

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: _start(), range(10)))

    named = [t for t in threading.enumerate() if t.name == "concurrent-start"]
    assert len(named) == 1, f"expected exactly one daemon thread, found {len(named)}"
    flusher.stop()


def test_on_error_raising_does_not_hang_the_loop() -> None:
    """A broken on_error callback that itself raises must not kill the writer thread —
    otherwise _idle is never set again and every later drain()/stop() call hangs forever,
    exactly like an unguarded writer failure would."""
    written: list[int] = []

    def flaky_writer(payload: int) -> None:
        if payload == 1:
            raise RuntimeError("write failed")
        written.append(payload)

    def flaky_on_error(exc: BaseException) -> None:
        raise ValueError("on_error itself is broken")

    flusher = LatestValueFlusher(
        flaky_writer, name="flaky-on-error", on_error=flaky_on_error
    ).start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True, "drain() must not hang after on_error raises"
    assert flusher.is_alive(), "the writer thread must survive a broken on_error callback"

    flusher.enqueue(2)
    assert flusher.drain(timeout=5) is True
    flusher.stop()
    assert written == [2]


def test_stop_with_zero_join_timeout_still_rejects_a_racing_enqueue() -> None:
    """join_timeout=0 makes stop() poll join() as aggressively as possible instead of
    waiting a full interval — stop() must still not return until the writer thread is
    truly dead, so a racing enqueue() right after stop() returns must be rejected."""
    flusher = LatestValueFlusher(lambda payload: None, name="zero-timeout", join_timeout=0.0)
    flusher.start()
    flusher.stop()
    with pytest.raises(RuntimeError):
        flusher.enqueue(1)


def test_stop_with_zero_join_timeout_never_leaves_two_live_writers() -> None:
    """stop() must not return until the OLD writer thread has actually terminated — not
    merely until a (possibly instantly-timed-out) join() call returns. Otherwise an
    immediate start() clears the shared stop flag before the old thread observes it, the
    old thread loops back to wait for more work instead of exiting, and two threads end
    up alive simultaneously, both able to pull from the single-slot mailbox and invoke
    the writer concurrently — breaking the one-writer-at-a-time guarantee this whole
    "latest value wins" design depends on. join_timeout=0.0 makes the old race window as
    wide as possible (join() always returns instantly, real or not)."""
    flusher = LatestValueFlusher(lambda payload: None, name="dual-writer-check", join_timeout=0.0)
    for _ in range(20):  # repeat to make a narrow race window practically certain to show up
        flusher.start()
        flusher.stop()
        named = [t for t in threading.enumerate() if t.name == "dual-writer-check"]
        assert len(named) == 0, f"stop() returned with {len(named)} writer thread(s) still alive"

    flusher.start()
    named = [t for t in threading.enumerate() if t.name == "dual-writer-check"]
    assert len(named) == 1, f"expected exactly one writer thread, found {len(named)}"
    flusher.stop()


def test_start_after_stop_with_zero_join_timeout_still_works() -> None:
    """Restarting immediately after a stop() with an aggressive join-polling interval must
    still create a genuinely live, working flusher that delivers new payloads."""
    written: list[int] = []
    flusher = LatestValueFlusher(written.append, name="restart", join_timeout=0.0)
    flusher.start()
    flusher.stop()

    flusher.start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True
    assert written == [1], "the restarted flusher must actually deliver new payloads"
    flusher.stop()
    flusher.stop()


def test_stop_from_writer_thread_raises_instead_of_deadlocking() -> None:
    """A writer/on_error callback calling stop() on itself must fail fast — draining
    would otherwise wait for the mailbox to go idle, which on this same thread can only
    happen after this very stop() call returns, deadlocking the writer thread forever."""
    errors: list[BaseException] = []

    def writer(payload: int) -> None:
        raise RuntimeError("trigger on_error")

    def on_error(exc: BaseException) -> None:
        try:
            flusher.stop()
        except RuntimeError as e:
            errors.append(e)

    flusher = LatestValueFlusher(writer, name="self-stop", on_error=on_error).start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True, "drain() must not hang waiting on the self-call"
    assert len(errors) == 1
    assert "writer thread itself" in str(errors[0])
    flusher.stop()


def test_drain_from_writer_thread_raises_instead_of_deadlocking() -> None:
    """Same self-deadlock risk as stop(), for a bare drain() call from within writer."""
    errors: list[BaseException] = []

    def writer(payload: int) -> None:
        try:
            flusher.drain()
        except RuntimeError as e:
            errors.append(e)

    flusher = LatestValueFlusher(writer, name="self-drain").start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True
    assert len(errors) == 1
    flusher.stop()


def test_write_now_from_writer_thread_raises_instead_of_deadlocking() -> None:
    """Same self-deadlock risk as stop()/drain(), for write_now() called from writer."""
    errors: list[BaseException] = []

    def writer(payload: int) -> None:
        try:
            flusher.write_now(lambda: None)
        except RuntimeError as e:
            errors.append(e)

    flusher = LatestValueFlusher(writer, name="self-write-now").start()
    flusher.enqueue(1)
    assert flusher.drain(timeout=5) is True
    assert len(errors) == 1
    flusher.stop()


def test_write_now_serializes_against_a_concurrently_enqueued_write() -> None:
    """A payload enqueued while write_now(fn) is in flight must not reach the writer
    until fn has finished — otherwise a concurrently racing background write could land
    before, or interleaved with, the caller's own write. This is exactly the ordering bug
    write_now() exists to close (e.g. a fresh direct status write getting overwritten by
    a stale background graph-state write enqueued from another thread mid-fanout)."""
    order: list[str] = []
    log_lock = threading.Lock()
    fn_started = threading.Event()
    enqueued = threading.Event()

    def writer(payload: str) -> None:
        with log_lock:
            order.append(f"background:{payload}")

    flusher = LatestValueFlusher(writer, name="write-now-order").start()

    def _fn() -> None:
        fn_started.set()
        enqueued.wait(timeout=5)  # let the concurrent enqueue below land while we "write"
        time.sleep(0.05)
        with log_lock:
            order.append("external")

    t = threading.Thread(target=lambda: flusher.write_now(_fn))
    t.start()
    fn_started.wait(timeout=5)
    flusher.enqueue("stale")  # must not block, and must not be written until fn() is done
    enqueued.set()
    t.join(timeout=5)
    assert flusher.drain(timeout=5) is True
    flusher.stop()

    assert order == ["external", "background:stale"], (
        f"expected write_now's fn to land strictly before the concurrently enqueued "
        f"background write, got {order}"
    )


def test_write_now_does_not_block_a_concurrent_enqueue() -> None:
    """enqueue() must return immediately even while a slow write_now(fn) call is in
    flight — a concurrent graph/state mutation must never be delayed by a slow direct
    write; only the eventual background write of that mutation's payload is delayed."""
    started = threading.Event()
    release = threading.Event()

    def _slow_fn() -> None:
        started.set()
        release.wait(timeout=5)

    flusher = LatestValueFlusher(lambda payload: None, name="write-now-nonblock").start()
    t = threading.Thread(target=lambda: flusher.write_now(_slow_fn))
    t.start()
    started.wait(timeout=5)

    start_time = time.monotonic()
    flusher.enqueue("fast")
    elapsed = time.monotonic() - start_time
    assert elapsed < 0.5, "enqueue() must never block on a concurrent write_now() call"

    release.set()
    t.join(timeout=5)
    assert flusher.drain(timeout=5) is True
    flusher.stop()
