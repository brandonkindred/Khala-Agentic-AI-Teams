"""Unit tests for the asyncio-native :func:`schedule_periodic_reap` / :func:`stop_periodic_reap`."""

from __future__ import annotations

import asyncio
import logging

import pytest

from shared.job_event_bus import BusState, subscribe
from shared.job_event_bus import scheduler as scheduler_module
from shared.job_event_bus.scheduler import schedule_periodic_reap, stop_periodic_reap


@pytest.mark.asyncio
async def test_invokes_reap_once_on_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    state = BusState()
    calls: list[tuple[float, int, str]] = []

    def fake_reap_once(passed_state, *, ttl_seconds, max_jobs, logger, label):
        assert passed_state is state
        calls.append((ttl_seconds, max_jobs, label))
        return (0, 0)

    monkeypatch.setattr(scheduler_module, "reap_once", fake_reap_once)

    task = schedule_periodic_reap(
        state, ttl_seconds=3600.0, max_jobs=1024, interval_seconds=0.01, label="test-bus"
    )
    try:
        await asyncio.sleep(0.06)  # several intervals' worth of real time
        assert len(calls) >= 2, f"expected several reap passes, got {len(calls)}"
        assert all(c == (3600.0, 1024, "test-bus") for c in calls)
    finally:
        await stop_periodic_reap(task)


@pytest.mark.asyncio
async def test_resolves_tunables_live_from_callables(monkeypatch: pytest.MonkeyPatch) -> None:
    state = BusState()
    seen_caps: list[int] = []

    def fake_reap_once(passed_state, *, ttl_seconds, max_jobs, logger, label):
        seen_caps.append(max_jobs)
        return (0, 0)

    monkeypatch.setattr(scheduler_module, "reap_once", fake_reap_once)
    cap = {"value": 10}
    task = schedule_periodic_reap(state, ttl_seconds=1.0, max_jobs=lambda: cap["value"], interval_seconds=0.01)
    try:
        await asyncio.sleep(0.03)
        cap["value"] = 20
        await asyncio.sleep(0.03)
    finally:
        await stop_periodic_reap(task)
    assert 10 in seen_caps, f"expected an early pass to read cap=10, saw {seen_caps}"
    assert 20 in seen_caps, f"expected a later pass to read the retuned cap=20, saw {seen_caps}"


@pytest.mark.asyncio
async def test_stop_cancels_cleanly_and_leaves_no_dangling_task() -> None:
    state = BusState()
    task = schedule_periodic_reap(state, ttl_seconds=1.0, max_jobs=1, interval_seconds=1.0)
    assert task in asyncio.all_tasks()

    await stop_periodic_reap(task)

    assert task.done()
    assert task.cancelled()
    assert task not in asyncio.all_tasks()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    state = BusState()
    task = schedule_periodic_reap(state, ttl_seconds=1.0, max_jobs=1, interval_seconds=1.0)
    await stop_periodic_reap(task)
    await stop_periodic_reap(task)  # already done -> no-op, must not raise


@pytest.mark.asyncio
async def test_failing_pass_is_logged_and_swallowed_and_loop_keeps_going(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    state = BusState()
    calls: list[int] = []

    def flaky_reap_once(passed_state, *, ttl_seconds, max_jobs, logger, label):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return (0, 0)

    monkeypatch.setattr(scheduler_module, "reap_once", flaky_reap_once)
    with caplog.at_level(logging.ERROR):
        task = schedule_periodic_reap(state, ttl_seconds=1.0, max_jobs=1, interval_seconds=0.01)
        try:
            await asyncio.sleep(0.06)
            assert len(calls) >= 2, "one raising pass must not kill the loop"
        finally:
            await stop_periodic_reap(task)
    assert "periodic reap iteration failed" in caplog.text


@pytest.mark.asyncio
async def test_evicts_stale_subscription_without_manual_reap_call() -> None:
    """Regression: the SCHEDULED task (its own timer, real reap_once — no
    monkeypatch) must actually evict stale state, proving the asyncio loop
    does real work on its own cadence rather than just invoking a mock."""
    state = BusState()
    subscribe(state, "j1").last_activity -= 1e9  # ancient -> past any TTL
    task = schedule_periodic_reap(state, ttl_seconds=3600.0, max_jobs=1024, interval_seconds=0.01)
    try:
        await asyncio.sleep(0.06)  # several intervals' worth of real time for the task to fire
        assert "j1" not in state.subscribers, "scheduled task never evicted the stale job"
    finally:
        await stop_periodic_reap(task)


@pytest.mark.parametrize("bad_interval", [0, -1.0])
def test_rejects_nonpositive_interval(bad_interval: float) -> None:
    with pytest.raises(ValueError):
        schedule_periodic_reap(BusState(), ttl_seconds=1.0, max_jobs=1, interval_seconds=bad_interval)
