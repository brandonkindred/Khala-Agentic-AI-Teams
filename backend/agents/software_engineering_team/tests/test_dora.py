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
    """Empty event list yields an all-zero DoraMetrics."""
    m = compute_from_events([], 30.0)
    assert m.deployment_count == 0
    assert m.deployment_frequency_per_day == 0.0
    assert m.lead_time_seconds_median is None
    assert m.lead_time_sample_count == 0
    assert m.change_failure_rate == 0.0
    assert m.mttr_seconds_median is None
    assert m.total_cost_usd == 0.0


def test_window_days_must_be_positive() -> None:
    """A non-positive window_days raises ValueError."""
    with pytest.raises(ValueError):
        compute_from_events([], 0)


def test_deployment_frequency() -> None:
    """Deployment frequency is merge count divided by the window in days."""
    events = [_ev(se_events.MERGE_TO_MAIN, offset_s=i) for i in range(6)]
    m = compute_from_events(events, 3.0)
    assert m.deployment_count == 6
    assert m.deployment_frequency_per_day == pytest.approx(2.0)


def test_lead_time_median() -> None:
    """Lead time is the median of per-task creation-to-merge durations."""
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
    """Lead time uses the earliest creation per task and ignores merges with no creation."""
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
    """Change failure rate is gate re-entries divided by merged tasks."""
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
    """MTTR pairs crash-detected with crash-resolved per job and takes the median."""
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
    """A crash-resolved with no matching crash-detected is ignored for MTTR."""
    events = [_ev(se_events.CRASH_RESOLVED, offset_s=10, job_id="jX")]
    m = compute_from_events(events, 30.0)
    assert m.crash_resolved_count == 0
    assert m.mttr_seconds_median is None


def test_cost_is_folded_in() -> None:
    """Supplied cost is folded in, rounded, and exposed as total and per-job."""
    cost = {"total_cost_usd": 1.2345678, "by_job": {"j1": 0.5, "j2": 0.7}}
    m = compute_from_events([], 30.0, cost=cost)
    assert m.total_cost_usd == pytest.approx(1.234568)
    assert m.cost_by_job == {"j1": 0.5, "j2": 0.7}


def test_change_failure_rate_clamped_to_one() -> None:
    """Change failure rate is clamped to 1.0 when re-entries exceed merged tasks."""
    # Distinct re-entry tasks can exceed merged tasks at a window edge → clamp to 1.0.
    events = [
        _ev(se_events.TASK_MERGED, task_id="t1", job_id="j"),
        _ev(se_events.GATE_REENTRY, task_id="t1", job_id="j"),
        _ev(se_events.GATE_REENTRY, task_id="t2", job_id="j"),
        _ev(se_events.GATE_REENTRY, task_id="t3", job_id="j"),
    ]
    m = compute_from_events(events, 30.0)
    assert m.merged_count == 1
    assert m.gate_reentry_count == 3  # three distinct tasks
    assert m.change_failure_rate == 1.0  # clamped from 3.0


def test_gate_reentry_deduplicated_per_job_task() -> None:
    """Repeated gate re-entries for the same (job, task) count only once."""
    # Re-emitted re-entries for the same (job, task) — e.g. on resume — count once.
    events = [
        _ev(se_events.TASK_MERGED, task_id="t1", job_id="j"),
        _ev(se_events.GATE_REENTRY, task_id="t1", job_id="j"),
        _ev(se_events.GATE_REENTRY, task_id="t1", job_id="j"),
    ]
    m = compute_from_events(events, 30.0)
    assert m.merged_count == 1
    assert m.gate_reentry_count == 1  # deduped
    assert m.change_failure_rate == pytest.approx(1.0)


def test_merged_count_deduplicated_by_job_and_task_id() -> None:
    """A task merged twice within one job counts once, using the earliest merge."""
    # A task re-queued after repair and merged twice (same job) counts once...
    events = [
        _ev(se_events.TASK_CREATED, offset_s=0, task_id="t1", job_id="j"),
        _ev(se_events.TASK_MERGED, offset_s=100, task_id="t1", job_id="j"),
        _ev(se_events.TASK_MERGED, offset_s=300, task_id="t1", job_id="j"),
    ]
    m = compute_from_events(events, 30.0)
    assert m.merged_count == 1
    assert m.lead_time_sample_count == 1
    assert m.lead_time_seconds_median == pytest.approx(100.0)  # earliest merge only


def test_same_task_id_across_jobs_not_collapsed() -> None:
    """The same task id in two different jobs counts as two merges."""
    # ...but two DIFFERENT jobs reusing a generic id ('task-1') must both count.
    events = [
        _ev(se_events.TASK_CREATED, offset_s=0, task_id="task-1", job_id="jA"),
        _ev(se_events.TASK_MERGED, offset_s=100, task_id="task-1", job_id="jA"),
        _ev(se_events.TASK_CREATED, offset_s=0, task_id="task-1", job_id="jB"),
        _ev(se_events.TASK_MERGED, offset_s=200, task_id="task-1", job_id="jB"),
    ]
    m = compute_from_events(events, 30.0)
    assert m.merged_count == 2
    assert m.lead_time_sample_count == 2
    assert m.lead_time_seconds_median == pytest.approx(150.0)  # median(100, 200)


def test_lead_time_uses_created_ts_from_detail_across_boundary() -> None:
    """Lead time falls back to the merge's detail.created_ts when no creation event is in window."""
    # No TASK_CREATED event in the window, but the merge carries its own created_ts.
    created = _BASE
    merged = _BASE + timedelta(seconds=900)
    ev = {
        "ts": merged,
        "event_type": se_events.TASK_MERGED,
        "job_id": "j",
        "task_id": "t1",
        "phase": "execution",
        "gate": "",
        "detail": {"created_ts": created.isoformat()},
    }
    m = compute_from_events([ev], 30.0)
    assert m.lead_time_sample_count == 1
    assert m.lead_time_seconds_median == pytest.approx(900.0)


def test_mttr_pairs_by_task_id_not_just_job() -> None:
    """MTTR pairs crashes by (job, task) so interleaved crashes are matched correctly."""
    # Interleaved crashes in one job; FIFO-by-job would mis-pair and skew the median.
    events = [
        _ev(se_events.CRASH_DETECTED, offset_s=0, job_id="j", task_id="A"),
        _ev(se_events.CRASH_DETECTED, offset_s=10, job_id="j", task_id="B"),
        _ev(se_events.CRASH_DETECTED, offset_s=20, job_id="j", task_id="C"),
        _ev(se_events.CRASH_RESOLVED, offset_s=21, job_id="j", task_id="C"),  # C: 1s
        _ev(se_events.CRASH_RESOLVED, offset_s=30, job_id="j", task_id="B"),  # B: 20s
        _ev(se_events.CRASH_RESOLVED, offset_s=100, job_id="j", task_id="A"),  # A: 100s
    ]
    m = compute_from_events(events, 30.0)
    assert m.crash_resolved_count == 3
    assert m.mttr_seconds_median == pytest.approx(20.0)  # median(1, 20, 100)


def test_to_dict_round_trips_fields() -> None:
    """to_dict exposes deployment_count, window_days, and computed_at."""
    m = compute_from_events([_ev(se_events.MERGE_TO_MAIN)], 1.0)
    d = m.to_dict()
    assert d["deployment_count"] == 1
    assert d["window_days"] == 1.0
    assert "computed_at" in d
