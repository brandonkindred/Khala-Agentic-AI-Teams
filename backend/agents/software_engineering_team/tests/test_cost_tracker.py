"""Unit tests for the SE per-job cost tracker (shared.cost_tracker)."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from software_engineering_team.shared import cost_tracker


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Reset module state and capture flushes instead of hitting the job store."""
    cost_tracker._states.clear()
    flushes: list[tuple[str, float]] = []
    monkeypatch.setattr(
        cost_tracker, "_flush_to_job_store", lambda job_id, total: flushes.append((job_id, total))
    )
    # Flush on every call so the throttle never hides a flush in fast tests.
    monkeypatch.setenv("SE_COST_FLUSH_INTERVAL_S", "0")
    yield flushes
    cost_tracker._states.clear()


def test_accumulates_and_returns_total() -> None:
    assert cost_tracker.add_cost("job-1", 0.5) == pytest.approx(0.5)
    assert cost_tracker.add_cost("job-1", 0.25) == pytest.approx(0.75)
    assert cost_tracker.get_cost("job-1") == pytest.approx(0.75)


def test_jobs_are_isolated() -> None:
    cost_tracker.add_cost("job-a", 1.0)
    cost_tracker.add_cost("job-b", 2.0)
    assert cost_tracker.get_cost("job-a") == pytest.approx(1.0)
    assert cost_tracker.get_cost("job-b") == pytest.approx(2.0)


def test_get_cost_untracked_is_zero() -> None:
    assert cost_tracker.get_cost("nope") == 0.0


def test_reset_clears_job() -> None:
    cost_tracker.add_cost("job-1", 1.0)
    cost_tracker.reset("job-1")
    assert cost_tracker.get_cost("job-1") == 0.0


def test_flush_happens(_clean_state) -> None:
    cost_tracker.add_cost("job-1", 0.5)
    assert ("job-1", pytest.approx(0.5)) in _clean_state


def test_force_flush(_clean_state, monkeypatch) -> None:
    monkeypatch.setenv("SE_COST_FLUSH_INTERVAL_S", "9999")  # throttle would block auto-flush
    cost_tracker.add_cost("job-1", 0.5)  # first call still flushes (last_flushed_at == 0)
    _clean_state.clear()
    cost_tracker.add_cost("job-1", 0.5)  # throttled — no flush
    assert _clean_state == []
    cost_tracker.flush("job-1")
    assert _clean_state == [("job-1", pytest.approx(1.0))]


def test_force_flush_untracked_is_noop(_clean_state) -> None:
    cost_tracker.flush("nope")
    assert _clean_state == []


def test_preconditions() -> None:
    with pytest.raises(ValueError):
        cost_tracker.add_cost("", 1.0)
    with pytest.raises(ValueError):
        cost_tracker.add_cost("job-1", -0.1)


def test_concurrent_accumulation_is_consistent() -> None:
    def worker() -> None:
        for _ in range(100):
            cost_tracker.add_cost("job-x", 0.01)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 8 threads * 100 * 0.01 = 8.0
    assert cost_tracker.get_cost("job-x") == pytest.approx(8.0)


@dataclass
class _FakeRecord:
    job_id: str | None
    team: str
    cost_usd: float


def test_observer_only_counts_se_jobs() -> None:
    cost_tracker._cost_observer(
        _FakeRecord(job_id="job-1", team="software_engineering", cost_usd=0.5)
    )
    cost_tracker._cost_observer(
        _FakeRecord(job_id="job-1", team="software_engineering_team", cost_usd=0.5)
    )
    assert cost_tracker.get_cost("job-1") == pytest.approx(1.0)


def test_observer_ignores_other_teams_and_missing_job() -> None:
    cost_tracker._cost_observer(_FakeRecord(job_id="job-2", team="blogging", cost_usd=5.0))
    cost_tracker._cost_observer(_FakeRecord(job_id=None, team="software_engineering", cost_usd=5.0))
    cost_tracker._cost_observer(
        _FakeRecord(job_id="job-3", team="software_engineering", cost_usd=0.0)
    )
    assert cost_tracker.get_cost("job-2") == 0.0
    assert cost_tracker.get_cost("job-3") == 0.0


def test_register_cost_observer_idempotent(monkeypatch) -> None:
    registered: list = []
    monkeypatch.setattr(cost_tracker, "_registered", False)
    import llm_service

    monkeypatch.setattr(llm_service, "register_call_observer", lambda obs: registered.append(obs))
    cost_tracker.register_cost_observer()
    cost_tracker.register_cost_observer()
    assert len(registered) == 1
