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
    events, nxt = tracker.events_since(0)
    assert len(events) == 2
    assert nxt == 2
    events_after_one, nxt2 = tracker.events_since(1)
    assert len(events_after_one) == 1
    assert nxt2 == 2


def test_events_since_threaded_index_never_duplicates_across_wrap(monkeypatch):
    # Simulate the SSE consumer: thread next_index back each tick. Even across an
    # eviction wrap, no event is ever returned twice.
    monkeypatch.setenv("SE_EXECUTION_TRACKER_EVENT_CAP", "100")
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")

    index = 0
    seen = 0
    events, index = tracker.events_since(index)
    seen += len(events)
    # Emit far past the cap, draining like the consumer does between emits.
    for i in range(500):
        tracker.observe_loop("t1", 1)
        if i % 50 == 0:
            events, index = tracker.events_since(index)
            seen += len(events)
    events, index = tracker.events_since(index)
    seen += len(events)
    # Never more than the total emitted (1 upsert + 500 loops); no re-emission.
    assert seen <= 501
    # Caller is now caught up: nothing new.
    assert tracker.events_since(index)[0] == []


def test_events_are_bounded_by_cap(monkeypatch):
    # The event buffer must not grow without bound; once full it drops the oldest.
    monkeypatch.setenv("SE_EXECUTION_TRACKER_EVENT_CAP", "100")
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    for _ in range(500):
        tracker.observe_loop("t1", 1)
    # 1 upsert + 500 loop events emitted, but buffer is capped at 100.
    assert len(tracker._events) == 100
    # event_count reflects the TOTAL emitted (so the SSE index stays meaningful).
    assert tracker.snapshot()["event_count"] == 501


def test_events_since_offsets_for_evicted(monkeypatch):
    # After eviction, events_since(index) must still return the right tail using the
    # total-emitted index the SSE consumer carries (not the buffer position).
    monkeypatch.setenv("SE_EXECUTION_TRACKER_EVENT_CAP", "100")
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    for _ in range(500):
        tracker.observe_loop("t1", 1)
    total = tracker.snapshot()["event_count"]  # 501
    # Asking from the very latest index returns nothing new, and next_index == total.
    assert tracker.events_since(total) == ([], total)
    # Asking from total-2 returns exactly the last 2 buffered events.
    assert len(tracker.events_since(total - 2)[0]) == 2
    # A consumer that fell behind the eviction window still gets the buffer (no crash),
    # and next_index jumps to total so it never re-reads the lost range.
    behind_events, behind_next = tracker.events_since(0)
    assert len(behind_events) == 100
    assert behind_next == total


def test_tasks_are_bounded_by_cap(monkeypatch):
    # Tasks left over from earlier jobs must not accumulate forever.
    monkeypatch.setenv("SE_EXECUTION_TRACKER_TASK_CAP", "100")
    tracker = ExecutionTracker()
    for i in range(250):
        tracker.upsert_task(f"t{i}", "x", "a")
    assert len(tracker._tasks) == 100
    # FIFO eviction keeps the most recently inserted tasks.
    task_ids = {t["task_id"] for t in tracker.snapshot()["tasks"]}
    assert "t249" in task_ids
    assert "t0" not in task_ids


def test_existing_task_update_does_not_count_against_cap(monkeypatch):
    monkeypatch.setenv("SE_EXECUTION_TRACKER_TASK_CAP", "100")
    tracker = ExecutionTracker()
    tracker.upsert_task("keep", "x", "a")
    for _ in range(500):
        tracker.upsert_task("keep", "x", "a")  # updates, never grows the dict
    assert len(tracker._tasks) == 1


def test_reset_clears_state(monkeypatch):
    tracker = ExecutionTracker()
    tracker.upsert_task("t1", "x", "a")
    tracker.observe_loop("t1", 1)
    tracker.reset()
    snap = tracker.snapshot()
    assert snap["tasks"] == []
    assert snap["event_count"] == 0
    assert tracker.events_since(0) == ([], 0)


def test_event_cap_env_defensive(monkeypatch):
    # Garbage -> default; below the floor -> clamped up.
    monkeypatch.setenv("SE_EXECUTION_TRACKER_EVENT_CAP", "not-a-number")
    assert ExecutionTracker()._event_cap == 5000
    monkeypatch.setenv("SE_EXECUTION_TRACKER_EVENT_CAP", "5")
    assert ExecutionTracker()._event_cap == 100  # clamped to _MIN_CAP
