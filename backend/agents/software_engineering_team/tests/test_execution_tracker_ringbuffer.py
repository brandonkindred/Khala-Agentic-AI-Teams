"""ExecutionTracker keeps a bounded event log without breaking absolute-index polling."""

import pytest

from software_engineering_team.shared.execution_tracker import _MAX_EVENTS, ExecutionTracker


def test_events_since_rejects_negative_index():
    t = ExecutionTracker()
    with pytest.raises(AssertionError):
        t.events_since(-1)


def test_event_log_is_bounded_but_count_is_monotonic():
    t = ExecutionTracker()
    n = _MAX_EVENTS + 500
    for _ in range(n):
        t.upsert_task("task", "title", "agent")  # one "task_upserted" event each

    snap = t.snapshot()
    # The reported count is the monotonic total, not the (capped) buffer size.
    assert snap["event_count"] == n
    # The retained buffer is capped at _MAX_EVENTS.
    assert len(t.events_since(0)) == _MAX_EVENTS


def test_events_since_preserves_absolute_index_across_eviction():
    t = ExecutionTracker()
    n = _MAX_EVENTS + 500
    for _ in range(n):
        t.upsert_task("task", "title", "agent")

    # At/after the head → nothing new.
    assert t.events_since(n) == []
    # The last 3 absolute positions are still retained.
    assert len(t.events_since(n - 3)) == 3
    # An index older than the retained window resumes from the oldest retained
    # event (no crash, no duplicates) rather than replaying evicted history.
    assert len(t.events_since(0)) == _MAX_EVENTS


def test_small_log_behaves_like_a_plain_list():
    t = ExecutionTracker()
    for _ in range(5):
        t.upsert_task("task", "title", "agent")
    assert t.snapshot()["event_count"] == 5
    assert len(t.events_since(0)) == 5
    assert len(t.events_since(2)) == 3
    assert t.events_since(5) == []
