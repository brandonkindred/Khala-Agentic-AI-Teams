"""Unit tests for DORA metric computation (metrics.dora.compute_from_events)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from software_engineering_team.metrics.dora import compute_from_events
from software_engineering_team.shared import se_events

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ev(event_type: str, *, offset_s: float = 0.0, job_id: str = "", task_id: str = "") -> dict:
    return {
        "ts": _BASE + timedelta(seconds=offset_s),
        "event_type": event_type,
        "job_id": job_id,
        "task_id": task_id,
        "phase": "",
        "gate": "",
        "detail": {},
    }


def test_empty_events_are_all_zero() -> None:
    m = compute_from_events([], 30.0)
    assert m.deployment_count == 0
    assert m.deployment_frequency_per_day == 0.0
    assert m.lead_time_seconds_median is None
    assert m.lead_time_sample_count == 0
    assert m.change_failure_rate == 0.0
    assert m.mttr_seconds_median is None
    assert m.total_cost_usd == 0.0


def test_window_days_must_be_positive() -> None:
    with pytest.raises(ValueError):
        compute_from_events([], 0)


def test_deployment_frequency() -> None:
    events = [_ev(se_events.MERGE_TO_MAIN, offset_s=i) for i in range(6)]
    m = compute_from_events(events, 3.0)
    assert m.deployment_count == 6
    assert m.deployment_frequency_per_day == pytest.approx(2.0)


def test_lead_time_median() -> None:
    events = [
        _ev(se_events.TASK_CREATED, offset_s=0, task_id="t1"),
        _ev(se_events.TASK_MERGED, offset_s=100, task_id="t1"),
        _ev(se_events.TASK_CREATED, offset_s=0, task_id="t2"),
        _ev(se_events.TASK_MERGED, offset_s=300, task_id="t2"),
    ]
    m = compute_from_events(events, 30.0)
    assert m.merged_count == 2
    assert m.lead_time_sample_count == 2
    assert m.lead_time_seconds_median == pytest.approx(200.0)  # median(100, 300)


def test_lead_time_uses_earliest_creation_and_ignores_unmatched() -> None:
    events = [
        _ev(se_events.TASK_CREATED, offset_s=50, task_id="t1"),
        _ev(se_events.TASK_CREATED, offset_s=10, task_id="t1"),  # earlier wins
        _ev(se_events.TASK_MERGED, offset_s=110, task_id="t1"),
        _ev(se_events.TASK_MERGED, offset_s=200, task_id="orphan"),  # no creation → ignored
    ]
    m = compute_from_events(events, 30.0)
    assert m.merged_count == 2
    assert m.lead_time_sample_count == 1
    assert m.lead_time_seconds_median == pytest.approx(100.0)  # 110 - 10


def test_change_failure_rate() -> None:
    events = [
        _ev(se_events.TASK_MERGED, task_id="t1"),
        _ev(se_events.TASK_MERGED, task_id="t2"),
        _ev(se_events.TASK_MERGED, task_id="t3"),
        _ev(se_events.TASK_MERGED, task_id="t4"),
        _ev(se_events.GATE_REENTRY, task_id="t1"),
    ]
    m = compute_from_events(events, 30.0)
    assert m.merged_count == 4
    assert m.gate_reentry_count == 1
    assert m.change_failure_rate == pytest.approx(0.25)


def test_mttr_pairs_per_job() -> None:
    events = [
        _ev(se_events.CRASH_DETECTED, offset_s=0, job_id="j1"),
        _ev(se_events.CRASH_RESOLVED, offset_s=60, job_id="j1"),
        _ev(se_events.CRASH_DETECTED, offset_s=0, job_id="j2"),
        _ev(se_events.CRASH_RESOLVED, offset_s=180, job_id="j2"),
    ]
    m = compute_from_events(events, 30.0)
    assert m.crash_resolved_count == 2
    assert m.mttr_seconds_median == pytest.approx(120.0)  # median(60, 180)


def test_resolved_without_detected_is_ignored() -> None:
    events = [_ev(se_events.CRASH_RESOLVED, offset_s=10, job_id="jX")]
    m = compute_from_events(events, 30.0)
    assert m.crash_resolved_count == 0
    assert m.mttr_seconds_median is None


def test_cost_is_folded_in() -> None:
    cost = {"total_cost_usd": 1.2345678, "by_job": {"j1": 0.5, "j2": 0.7}}
    m = compute_from_events([], 30.0, cost=cost)
    assert m.total_cost_usd == pytest.approx(1.234568)
    assert m.cost_by_job == {"j1": 0.5, "j2": 0.7}


def test_to_dict_round_trips_fields() -> None:
    m = compute_from_events([_ev(se_events.MERGE_TO_MAIN)], 1.0)
    d = m.to_dict()
    assert d["deployment_count"] == 1
    assert d["window_days"] == 1.0
    assert "computed_at" in d
