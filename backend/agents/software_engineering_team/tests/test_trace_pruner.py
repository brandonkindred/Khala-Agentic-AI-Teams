"""Unit tests for the scheduled SE trace pruner (``trace_pruner``).

``trace_store.prune_traces()`` enforces ``SE_TRACE_RETENTION_DAYS`` but was
dead code until this heartbeat wired it up (mirrors ``trace_flusher``'s
``BackgroundHeartbeat`` pattern, at an hours-scale cadence instead of
``trace_flusher``'s ~2s). These tests pin the register/unregister/shutdown
lifecycle, the interval env parsing, and confirm a prune failure can never
propagate out of the heartbeat.
"""

from __future__ import annotations

import pytest

from software_engineering_team.shared import trace_pruner, trace_store


@pytest.fixture(autouse=True)
def _reset_pruner():
    """Start each test with no registered heartbeat; clean up afterward."""
    trace_pruner._reset_for_test()
    yield
    trace_pruner._reset_for_test()


def test_prune_interval_env_default_and_garbage(monkeypatch) -> None:
    """_prune_interval_s defaults to 21600 (6h), parses overrides, falls back
    to the default on garbage, and clamps negatives to the 0 floor."""
    monkeypatch.delenv("SE_TRACE_PRUNE_INTERVAL_S", raising=False)
    assert trace_pruner._prune_interval_s() == 21600.0
    monkeypatch.setenv("SE_TRACE_PRUNE_INTERVAL_S", "3600")
    assert trace_pruner._prune_interval_s() == 3600.0
    monkeypatch.setenv("SE_TRACE_PRUNE_INTERVAL_S", "garbage")
    assert trace_pruner._prune_interval_s() == 21600.0
    monkeypatch.setenv("SE_TRACE_PRUNE_INTERVAL_S", "-5")
    assert trace_pruner._prune_interval_s() == 0.0  # clamped to floor


def test_register_floors_interval_to_avoid_busy_loop(monkeypatch) -> None:
    """A 0/garbage-clamped interval is floored to 60s at registration so the
    heartbeat can never busy-loop."""
    monkeypatch.setenv("SE_TRACE_PRUNE_INTERVAL_S", "0")

    trace_pruner.register_trace_pruner()

    assert trace_pruner._heartbeat is not None
    assert trace_pruner._heartbeat._interval_s == 60.0


def test_prune_tick_calls_prune_traces_and_logs_when_removed(monkeypatch, caplog) -> None:
    """_prune_tick delegates to trace_store.prune_traces and logs the count at INFO."""
    caplog.set_level("INFO", logger="software_engineering_team.shared.trace_pruner")
    monkeypatch.setattr(trace_store, "prune_traces", lambda: 5)

    trace_pruner._prune_tick()

    assert any("pruned 5" in r.message for r in caplog.records)


def test_prune_tick_silent_when_nothing_removed(monkeypatch, caplog) -> None:
    """_prune_tick logs nothing when prune_traces removes 0 rows (avoids log
    spam on the common case of an already-clean table)."""
    caplog.set_level("INFO", logger="software_engineering_team.shared.trace_pruner")
    monkeypatch.setattr(trace_store, "prune_traces", lambda: 0)

    trace_pruner._prune_tick()

    assert not any("pruned" in r.message for r in caplog.records)


def test_prune_tick_failure_is_swallowed_by_heartbeat(monkeypatch) -> None:
    """A prune_traces failure never raises through the registered heartbeat.

    trace_store.prune_traces already swallows and logs its own failures in
    production; this simulates a hypothetical raise to prove the heartbeat
    layer is a genuine second line of defense, not just an assumption.
    """
    monkeypatch.setattr(
        trace_store, "prune_traces", lambda: (_ for _ in ()).throw(RuntimeError("pg down"))
    )
    trace_pruner.register_trace_pruner()

    # Invoke the constructed heartbeat's tick directly — proves the end-to-end
    # wiring is safe without re-deriving BackgroundHeartbeat's own (separately
    # tested) swallow-and-continue contract.
    still_running = trace_pruner._heartbeat._tick()

    assert still_running is True


def test_register_starts_heartbeat_idempotent(monkeypatch) -> None:
    """register_trace_pruner starts exactly one heartbeat even when called twice."""
    started: list = []
    real_start = trace_pruner.BackgroundHeartbeat.start

    def fake_start(self):
        started.append(self)
        return real_start(self)

    monkeypatch.setattr(trace_pruner.BackgroundHeartbeat, "start", fake_start)

    trace_pruner.register_trace_pruner()
    trace_pruner.register_trace_pruner()  # idempotent — second call is a no-op

    assert len(started) == 1
    assert trace_pruner._is_registered()


def test_register_propagates_heartbeat_construction_failure(monkeypatch) -> None:
    """Unlike trace_flusher, register_trace_pruner has no internal try/except:
    there is no external llm_service call to guard here, so a construction
    failure propagates to the caller (_se_startup's shared try/except around
    all telemetry registrations is the intended guard)."""

    def boom(*args, **kwargs):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(trace_pruner, "BackgroundHeartbeat", boom)

    with pytest.raises(RuntimeError):
        trace_pruner.register_trace_pruner()

    assert trace_pruner._is_registered() is False


def test_unregister_stops_heartbeat() -> None:
    """unregister stops the heartbeat and clears registration state."""
    trace_pruner._mark_registered_for_test()
    trace_pruner._set_heartbeat_for_test()

    trace_pruner.unregister()

    assert trace_pruner._is_registered() is False
    assert trace_pruner._heartbeat is None


def test_unregister_when_not_registered_is_noop() -> None:
    """unregister() on a fresh (unregistered) state is a no-op, not an error."""
    trace_pruner.unregister()  # must not raise
    assert trace_pruner._is_registered() is False


def test_shutdown_calls_unregister(monkeypatch) -> None:
    """shutdown() is a lifecycle alias for unregister() (no final drain needed
    — pruning is idempotent, unlike trace_flusher's buffered writes)."""
    called = {"unregister": False}
    monkeypatch.setattr(trace_pruner, "unregister", lambda: called.__setitem__("unregister", True))

    trace_pruner.shutdown()

    assert called["unregister"] is True


def test_shutdown_when_not_registered_is_noop() -> None:
    """shutdown() on a fresh (unregistered) state is a no-op, not an error."""
    trace_pruner.shutdown()  # must not raise
    assert trace_pruner._is_registered() is False
