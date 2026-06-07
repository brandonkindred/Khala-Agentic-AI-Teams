"""Unit tests for the shared :class:`BackgroundHeartbeat` driver.

These cover every variation point the three migrated callers rely on: beating
until stopped, prompt join on stop, error routing (swallow vs. on_error),
self-terminating ``should_continue``, predicate-error survival, context capture,
idempotent start, and context-manager use.
"""

from __future__ import annotations

import contextvars
import threading
import time

import pytest

from shared_temporal.heartbeat import BackgroundHeartbeat


def test_beats_until_stopped() -> None:
    """The beater fires repeatedly on its interval and halts on stop()."""
    beats = {"n": 0}
    hb = BackgroundHeartbeat(lambda: beats.__setitem__("n", beats["n"] + 1), 0.01).start()
    time.sleep(0.06)
    hb.stop()
    after_stop = beats["n"]
    assert after_stop >= 2, f"expected several beats, got {after_stop}"
    time.sleep(0.04)
    assert beats["n"] == after_stop, "no beats may fire after stop()"


def test_stop_joins_thread_promptly() -> None:
    """stop() sets the flag and joins; the thread is no longer alive afterwards."""
    hb = BackgroundHeartbeat(lambda: None, 0.01, name="join-test").start()
    time.sleep(0.02)
    hb.stop()
    alive = [t for t in threading.enumerate() if t.name == "join-test"]
    assert not alive, f"thread should have joined, still alive: {alive}"


def test_beat_errors_are_swallowed_by_default() -> None:
    """A raising beat (no on_error) does not kill the loop; it keeps beating."""
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("beat blew up")

    hb = BackgroundHeartbeat(boom, 0.01).start()
    time.sleep(0.06)
    hb.stop()
    assert calls["n"] >= 2, "loop must survive beat exceptions and continue"


def test_beat_errors_routed_to_on_error() -> None:
    """When on_error is supplied, every beat exception is delivered to it."""
    errors: list[BaseException] = []

    def boom() -> None:
        raise ValueError("nope")

    hb = BackgroundHeartbeat(boom, 0.01, on_error=errors.append).start()
    time.sleep(0.05)
    hb.stop()
    assert errors, "on_error must receive the beat exceptions"
    assert all(isinstance(e, ValueError) for e in errors)


def test_should_continue_self_terminates() -> None:
    """A should_continue predicate returning False exits the thread without a stop()."""
    state = {"active": True, "beats": 0}

    def beat() -> None:
        state["beats"] += 1
        # Go inactive after the first beat so the next tick's predicate stops it.
        state["active"] = False

    BackgroundHeartbeat(
        beat, 0.01, name="selfterm", should_continue=lambda: state["active"]
    ).start()
    time.sleep(0.08)
    # Thread should have exited on its own (predicate False), never calling stop().
    alive = [t for t in threading.enumerate() if t.name == "selfterm"]
    assert not alive, "thread should self-terminate when should_continue is False"
    assert state["beats"] == 1, "exactly one beat before the predicate went False"


def test_predicate_error_does_not_kill_loop() -> None:
    """A raising should_continue is routed to on_error and the loop continues."""
    errors: list[BaseException] = []
    ticks = {"n": 0}

    def flaky_predicate() -> bool:
        ticks["n"] += 1
        if ticks["n"] <= 2:
            raise RuntimeError("predicate transient")
        return False  # eventually stop so the test thread exits

    hb = BackgroundHeartbeat(
        lambda: None,
        0.01,
        name="pred-err",
        should_continue=flaky_predicate,
        on_error=errors.append,
    ).start()
    time.sleep(0.1)
    hb.stop()
    assert len(errors) >= 2, "predicate errors must be routed to on_error, not kill the loop"


def test_copy_context_runs_beat_in_snapshot() -> None:
    """copy_context=True runs the beat inside the constructor-thread's context snapshot."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("hb_var", default="unset")
    seen: list[str] = []

    var.set("snapshotted")
    hb = BackgroundHeartbeat(lambda: seen.append(var.get()), 0.01, copy_context=True).start()
    time.sleep(0.03)
    hb.stop()
    assert seen, "beat should have run"
    assert all(v == "snapshotted" for v in seen), f"context not captured: {seen}"


def test_start_is_idempotent() -> None:
    """Calling start() twice does not spawn a second thread for the same beater."""
    hb = BackgroundHeartbeat(lambda: None, 0.05, name="idem").start()
    hb.start()  # no-op while alive
    named = [t for t in threading.enumerate() if t.name == "idem"]
    assert len(named) == 1, f"expected one thread, found {len(named)}"
    hb.stop()


def test_context_manager_starts_and_stops() -> None:
    """The context-manager form starts on enter and joins on exit."""
    beats = {"n": 0}
    with BackgroundHeartbeat(lambda: beats.__setitem__("n", beats["n"] + 1), 0.01, name="ctx"):
        time.sleep(0.04)
    assert beats["n"] >= 2
    assert not [t for t in threading.enumerate() if t.name == "ctx"], "thread must join on exit"


def test_stop_before_start_is_safe() -> None:
    """stop() on a never-started beater is a no-op (no thread to join)."""
    hb = BackgroundHeartbeat(lambda: None, 0.01)
    hb.stop()  # must not raise


def test_invalid_args_rejected() -> None:
    """Preconditions: non-positive interval and non-callable beat are rejected."""
    with pytest.raises(AssertionError):
        BackgroundHeartbeat(lambda: None, 0.0)
    with pytest.raises(AssertionError):
        BackgroundHeartbeat(None, 1.0)  # type: ignore[arg-type]
