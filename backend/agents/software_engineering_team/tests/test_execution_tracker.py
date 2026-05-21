"""Unit tests for shared.execution_tracker."""

from __future__ import annotations

import pytest

from software_engineering_team.shared.execution_tracker import (
    ExecutionTask,
    ExecutionTracker,
    _iso,
    _utc_now,
)


def test_utc_now_returns_aware():
    ts = _utc_now()
    assert ts.tzinfo is not None


def test_iso_none():
    assert _iso(None) is None


def test_iso_with_ts():
    ts = _utc_now()
    out = _iso(ts)
    assert isinstance(out, str)
    assert "T" in out


def test_execution_task_to_dict_minimal():
    t = ExecutionTask(task_id="t1", title="x", assigned_agent="a")
    d = t.to_dict()
    assert d["task_id"] == "t1"
    assert d["loop_count_min"] == 0
    assert d["loop_count_max"] == 0
    assert d["loop_count_avg"] == 0.0
    assert d["duration_seconds"] is None


def test_execution_task_to_dict_with_loops_and_times():
    t = ExecutionTask(task_id="t1", title="x", assigned_agent="a", loop_counts=[1, 2, 5])
    t.started_at = _utc_now()
    t.finished_at = _utc_now()
    d = t.to_dict()
    assert d["loop_count_min"] == 1
    assert d["loop_count_max"] == 5
    assert d["loop_count_avg"] > 0
    assert d["duration_seconds"] is not None


def test_tracker_upsert_creates_task():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "title", "agent", ["dep1"])
    snap = tracker.snapshot()
    assert len(snap["tasks"]) == 1
    assert snap["tasks"][0]["task_id"] == "t1"


def test_tracker_upsert_updates_existing():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "title", "agent")
    tracker.upsert_task("t1", "new title", "new agent", ["new dep"])
    snap = tracker.snapshot()
    assert snap["tasks"][0]["title"] == "new title"
    assert snap["tasks"][0]["dependencies"] == ["new dep"]


def test_tracker_start_task():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.start_task("t1")
    snap = tracker.snapshot()
    assert snap["tasks"][0]["status"] == "in_progress"
    assert snap["tasks"][0]["started_at"] is not None
    assert snap["tasks"][0]["percent_complete"] >= 5.0


def test_tracker_start_unknown_task_silent():
    tracker = ExecutionTracker()
    tracker.start_task("missing")
    snap = tracker.snapshot()
    assert snap["tasks"] == []


def test_tracker_update_progress():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.update_progress("t1", 50.0)
    snap = tracker.snapshot()
    assert snap["tasks"][0]["percent_complete"] == 50.0
    assert snap["tasks"][0]["status"] != "done"


def test_tracker_update_progress_to_100_marks_done():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.update_progress("t1", 100.0)
    snap = tracker.snapshot()
    assert snap["tasks"][0]["status"] == "done"
    assert snap["tasks"][0]["finished_at"] is not None


def test_tracker_update_progress_clamps():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.update_progress("t1", -10)
    tracker.update_progress("t1", 200)
    snap = tracker.snapshot()
    assert snap["tasks"][0]["percent_complete"] == 100.0


def test_tracker_update_progress_unknown():
    tracker = ExecutionTracker()
    tracker.update_progress("missing", 50)
    assert tracker.snapshot()["tasks"] == []


def test_tracker_observe_loop():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.observe_loop("t1", 5)
    tracker.observe_loop("t1", 10)
    snap = tracker.snapshot()
    assert snap["tasks"][0]["loop_count_max"] == 10


def test_tracker_observe_loop_unknown():
    tracker = ExecutionTracker()
    tracker.observe_loop("missing", 5)
    assert tracker.snapshot()["tasks"] == []


def test_tracker_finish_task_done():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.finish_task("t1")
    snap = tracker.snapshot()
    assert snap["tasks"][0]["status"] == "done"
    assert snap["tasks"][0]["percent_complete"] == 100.0


def test_tracker_finish_task_blocked():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.update_progress("t1", 30)
    tracker.finish_task("t1", blocked=True)
    snap = tracker.snapshot()
    assert snap["tasks"][0]["status"] == "blocked"
    # percent_complete preserved (not forced to 100)
    assert snap["tasks"][0]["percent_complete"] == 30.0


def test_tracker_finish_task_unknown():
    tracker = ExecutionTracker()
    tracker.finish_task("missing")
    assert tracker.snapshot()["tasks"] == []


def test_tracker_snapshot_progress_calculation():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.upsert_task("t2", "y", "a")
    tracker.upsert_task("t3", "z", "a")
    tracker.update_progress("t1", 100)
    tracker.update_progress("t2", 100)
    snap = tracker.snapshot()
    assert snap["plan_progress_percent"] == pytest.approx(66.67, abs=0.1)


def test_tracker_snapshot_empty():
    tracker = ExecutionTracker()
    snap = tracker.snapshot()
    assert snap["plan_progress_percent"] == 0.0
    assert snap["tasks"] == []
    assert snap["event_count"] == 0


def test_tracker_events_since_index():
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.upsert_task("t2", "y", "a")
    events = tracker.events_since(0)
    assert len(events) == 2
    events_after_one = tracker.events_since(1)
    assert len(events_after_one) == 1
