"""Unit tests for the scheduled SE trace pruner (``trace_pruner``).

``trace_store.prune_traces()`` enforces ``SE_TRACE_RETENTION_DAYS`` but was
dead code until this heartbeat wired it up (mirrors ``trace_flusher``'s
``BackgroundHeartbeat`` pattern, at an hours-scale cadence instead of
``trace_flusher``'s ~2s). These tests pin the register/unregister lifecycle,
the interval env parsing, and confirm a heartbeat construction/start failure
can never propagate out of registration. A prune-tick failure never
propagating is BackgroundHeartbeat's own generic contract (see
``shared/concurrency/tests/test_heartbeat.py``) and isn't re-tested here,
matching ``trace_flusher``'s own convention of trusting that contract.
"""

from __future__ import annotations

import pytest

from software_engineering_team.shared import trace_pruner, trace_store


@pytest.fixture(autouse=True)
def _reset_pruner(monkeypatch):
    """Start each test with no registered heartbeat; clean up afterward.

    Also default-mocks trace_store.prune_traces to a harmless no-op: several
    tests call the real register_trace_pruner(), which (with beat_first=True)
    fires a real tick within the test's own runtime. Without this guard, a
    developer running this file with POSTGRES_HOST configured locally (a
    documented, legitimate local-dev setup) would have "pure bookkeeping"
    tests silently DELETE real se_agent_traces rows, since prune_traces()
    intentionally does not gate on SE_TRACE_TO_POSTGRES. Tests that care what
    prune_traces does override this locally.
    """
    monkeypatch.setattr(trace_store, "prune_traces", lambda: 0)
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


def test_register_floors_interval_regardless_of_why_its_low(monkeypatch) -> None:
    """The 60s floor applies to any resolved value below it, not just the
    negative/garbage-clamped-to-0 case — pinning both ends of the (0, 60)
    band so an implementation that only special-cases <=0 (e.g. `interval if
    interval > 0 else 60.0` instead of `max(interval, 60.0)`) would fail here
    even though it'd pass a zero-only check."""
    monkeypatch.setenv("SE_TRACE_PRUNE_INTERVAL_S", "0")
    trace_pruner.register_trace_pruner()
    assert trace_pruner._heartbeat._interval_s == 60.0

    trace_pruner._reset_for_test()
    monkeypatch.setenv("SE_TRACE_PRUNE_INTERVAL_S", "30")
    trace_pruner.register_trace_pruner()
    assert trace_pruner._heartbeat._interval_s == 60.0


def test_register_configures_beat_first_and_callable() -> None:
    """register_trace_pruner enables beat_first (so a restart landing more
    often than the interval still gets a sweep in) and hands _prune_tick to
    the heartbeat as its beat callable — the actual wiring this PR exists to
    establish. A mis-wired or no-op registration would otherwise pass every
    other test in this file while production pruning silently never ran."""
    trace_pruner.register_trace_pruner()

    assert trace_pruner._heartbeat._beat_first is True
    assert trace_pruner._heartbeat._beat is trace_pruner._prune_tick


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


def test_register_starts_heartbeat_idempotent() -> None:
    """register_trace_pruner starts exactly one heartbeat even when called twice."""
    trace_pruner.register_trace_pruner()
    first = trace_pruner._heartbeat

    trace_pruner.register_trace_pruner()  # idempotent — second call is a no-op

    assert trace_pruner._heartbeat is first
    assert trace_pruner._is_registered()


def test_register_swallows_heartbeat_construction_failure(monkeypatch, caplog) -> None:
    """A BackgroundHeartbeat construction/start failure is caught and logged,
    not propagated — so it can't abort registrations that run after this one
    in _se_startup's shared try/except (e.g. register_transcript_flusher)."""
    caplog.set_level("WARNING", logger="software_engineering_team.shared.trace_pruner")

    def boom(*args, **kwargs):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(trace_pruner, "BackgroundHeartbeat", boom)

    trace_pruner.register_trace_pruner()  # must not raise

    assert trace_pruner._is_registered() is False
    assert any("could not start" in r.message for r in caplog.records)


def test_unregister_stops_heartbeat() -> None:
    """unregister stops the heartbeat and clears registration state."""
    trace_pruner.register_trace_pruner()

    trace_pruner.unregister()

    assert trace_pruner._is_registered() is False


def test_unregister_when_not_registered_is_noop() -> None:
    """unregister() on a fresh (unregistered) state is a no-op, not an error."""
    trace_pruner.unregister()  # must not raise
    assert trace_pruner._is_registered() is False
