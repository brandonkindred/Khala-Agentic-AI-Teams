"""Unit tests for the reusable :class:`ReaperHandle` (DB-free, thread-only)."""

from __future__ import annotations

import threading
import time

from shared.job_event_bus import BusState, ReaperHandle, subscribe


def _make_handle(state: BusState, **overrides) -> ReaperHandle:
    kwargs = dict(
        ttl_seconds=3600.0,
        max_jobs=1024,
        interval_seconds=300.0,
        name="test-reaper",
        label="test event-bus",
    )
    kwargs.update(overrides)
    return ReaperHandle(state, **kwargs)


def test_reap_once_evicts_idle_subscription() -> None:
    state = BusState()
    sub = subscribe(state, "j1")
    sub.last_activity -= 1e9  # ancient → past any TTL
    handle = _make_handle(state)
    jobs, subs = handle.reap_once()
    assert (jobs, subs) == (1, 1)
    assert "j1" not in state.subscribers


def test_reap_once_reads_tunables_live_from_callables() -> None:
    # Callable sources are resolved on each pass, so retuning affects the next reap.
    state = BusState()
    subscribe(state, "a")
    subscribe(state, "b")
    subscribe(state, "c")
    cap = {"value": 1024}
    handle = _make_handle(state, max_jobs=lambda: cap["value"])
    assert handle.reap_once() == (0, 0)  # cap not exceeded yet
    cap["value"] = 2
    jobs, subs = handle.reap_once()
    assert len(state.subscribers) == 2
    assert jobs == 1 and subs == 1
    assert "a" not in state.subscribers  # oldest evicted first


def test_ensure_started_is_idempotent_and_single_threaded() -> None:
    state = BusState()
    handle = _make_handle(state, name="idem-reaper")

    def _alive() -> int:
        return len([t for t in threading.enumerate() if t.name == "idem-reaper"])

    try:
        handle.ensure_started()
        handle.ensure_started()
        assert _alive() == 1
    finally:
        handle.shutdown()
    assert _alive() == 0


def test_concurrent_ensure_started_yields_one_reaper() -> None:
    state = BusState()
    handle = _make_handle(state, name="race-reaper")

    def _alive() -> int:
        return len([t for t in threading.enumerate() if t.name == "race-reaper"])

    barrier = threading.Barrier(16)

    def _racer() -> None:
        barrier.wait()
        handle.ensure_started()

    threads = [threading.Thread(target=_racer) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert _alive() == 1
    finally:
        handle.shutdown()
    assert _alive() == 0


def test_shutdown_is_idempotent_and_restartable() -> None:
    state = BusState()
    handle = _make_handle(state, name="restart-reaper")
    handle.shutdown()  # never started → no-op
    handle.ensure_started()
    handle.shutdown()
    handle.shutdown()  # already stopped → no-op
    # Re-startable after shutdown.
    handle.ensure_started()
    try:
        assert handle._reaper is not None and handle._reaper.is_alive()
    finally:
        handle.shutdown()


def test_ensure_started_evicts_stale_subscription_without_manual_reap_call() -> None:
    """Regression: the SCHEDULED reaper (its own timer, not a direct .reap_once()
    call) must actually evict stale state — proving the background thread does
    real work on its own cadence, not just that it starts and stops cleanly."""
    state = BusState()
    subscribe(state, "j1").last_activity -= 1e9  # ancient -> past any TTL
    handle = _make_handle(state, name="e2e-reaper", interval_seconds=0.01)
    try:
        handle.ensure_started()
        time.sleep(0.06)  # several intervals' worth of real time for the thread to fire
        assert "j1" not in state.subscribers, "scheduled reaper never evicted the stale job"
    finally:
        handle.shutdown()


def test_constructor_rejects_nonpositive_interval() -> None:
    import pytest

    with pytest.raises(AssertionError):
        ReaperHandle(
            BusState(),
            ttl_seconds=1.0,
            max_jobs=1,
            interval_seconds=0,
            name="bad",
        )
