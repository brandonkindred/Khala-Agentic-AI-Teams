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
    later drain()/stop() call forever."""
    flusher = LatestValueFlusher(lambda payload: None, name="not-started")
    with pytest.raises(AssertionError):
        flusher.enqueue(1)
    assert flusher.drain(timeout=1) is True, "idle flag must be untouched by the rejected enqueue"


def test_enqueue_after_stop_raises_instead_of_hanging() -> None:
    """enqueue() after stop() must also fail fast, not silently hang a later drain()."""
    flusher = LatestValueFlusher(lambda payload: None, name="post-stop").start()
    flusher.stop()
    with pytest.raises(AssertionError):
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
    """A join(timeout=0) can return before the OS thread has actually finished exiting —
    stop() must not let a concurrent enqueue() slip through during that window just
    because is_alive() might still (briefly) read True. join_timeout=0 makes this
    deterministic: join() always returns immediately, whether or not the thread is
    truly done, so any correct implementation must reject the racing enqueue anyway."""
    flusher = LatestValueFlusher(lambda payload: None, name="zero-timeout", join_timeout=0.0)
    flusher.start()
    flusher.stop()
    with pytest.raises(AssertionError):
        flusher.enqueue(1)


def test_start_after_stop_with_zero_join_timeout_still_works() -> None:
    """Restarting immediately after a stop() whose bounded join may not have observed the
    old thread's actual exit must still create a genuinely live, working flusher — not
    silently no-op against the (possibly still-finishing) old thread and leave the
    caller believing a new flusher is running when nothing is."""
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
