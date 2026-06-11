"""Tests for coding_team.activity.ActivityBridge — the shared progress bridge."""

from __future__ import annotations

from typing import Any, Dict, List

from coding_team.activity import ActivityBridge


def _collect(updates: List[Dict[str, Any]]):
    def _update(**kw: Any) -> None:
        updates.append(kw)

    return _update


def test_bridge_writes_schema_and_status_text():
    """A report writes status_text with a rounded percent plus the structured entry
    including task context when provided."""
    updates: List[Dict[str, Any]] = []
    bridge = ActivityBridge(
        _collect(updates),
        agent="code_review",
        label="Code review",
        task_id="t1",
        task_title="Add login",
        min_interval_s=0.0,
    )

    bridge("reviewing", "chunk 2/5", 0.366)

    assert len(updates) == 1
    kw = updates[0]
    assert kw["status_text"] == "Code review (37%): Add login — chunk 2/5"
    assert kw["current_activity"] == {
        "agent": "code_review",
        "step": "reviewing",
        "detail": "chunk 2/5",
        "fraction": 0.366,
        "task_id": "t1",
        "task_title": "Add login",
    }


def test_bridge_omits_task_fields_when_absent():
    """Sites without a task (e.g. /review-pr) produce an entry without task keys."""
    updates: List[Dict[str, Any]] = []
    bridge = ActivityBridge(
        _collect(updates), agent="code_review", label="Reviewing PR #7", min_interval_s=0.0
    )

    bridge("parsing", "parsing findings", 0.85)

    assert updates[0]["status_text"] == "Reviewing PR #7 (85%) — parsing findings"
    assert "task_id" not in updates[0]["current_activity"]
    assert "task_title" not in updates[0]["current_activity"]


def test_bridge_coalesces_same_step_reports():
    """Rapid same-step reports collapse to one write per interval; a step change or a
    terminal fraction always writes."""
    updates: List[Dict[str, Any]] = []
    bridge = ActivityBridge(
        _collect(updates), agent="code_review", label="Code review", min_interval_s=3600.0
    )

    bridge("reviewing", "chunk 1/9", 0.1)  # first report always writes
    bridge("reviewing", "chunk 2/9", 0.2)  # same step, inside interval → dropped
    bridge("reviewing", "chunk 3/9", 0.3)  # dropped
    assert len(updates) == 1

    bridge("parsing", "parsing findings", 0.9)  # step change → writes
    assert len(updates) == 2

    bridge("parsing", "still parsing", 0.95)  # same step, inside interval → dropped
    bridge("parsing", "done parsing", 1.0)  # terminal fraction → writes
    assert len(updates) == 3
    assert updates[-1]["current_activity"]["fraction"] == 1.0


def test_bridge_swallows_errors_and_cools_down():
    """A failing store write is swallowed and opens a cooldown window during which
    reports are skipped entirely — no repeated blocking writes against a dead store."""
    calls = {"n": 0}

    def _boom(**kw: Any) -> None:
        calls["n"] += 1
        raise RuntimeError("store down")

    bridge = ActivityBridge(
        _boom,
        agent="code_review",
        label="Code review",
        min_interval_s=0.0,
        failure_cooldown_s=3600.0,
    )

    bridge("reviewing", "a", 0.1)  # attempts, fails, opens cooldown — must not raise
    bridge("parsing", "b", 0.5)  # inside cooldown → skipped, no store call
    bridge("done", "c", 1.0)  # still skipped
    assert calls["n"] == 1


def test_bridge_clear_always_attempted_and_swallowed():
    """clear() runs even during a failure cooldown (a missed clear is worse than a
    missed report) and never raises."""
    updates: List[Dict[str, Any]] = []
    failures = {"armed": True}

    def _flaky(**kw: Any) -> None:
        if failures["armed"]:
            failures["armed"] = False
            raise RuntimeError("store down")
        updates.append(kw)

    bridge = ActivityBridge(
        _flaky,
        agent="code_review",
        label="Code review",
        min_interval_s=0.0,
        failure_cooldown_s=3600.0,
    )

    bridge("reviewing", "a", 0.1)  # fails → cooldown armed
    bridge.clear()  # must still be attempted, and succeeds
    assert updates == [{"current_activity": None}]

    def _always_boom(**kw: Any) -> None:
        raise RuntimeError("store down")

    ActivityBridge(_always_boom, agent="x", label="X").clear()  # must not raise


def test_bridge_clamps_out_of_range_fraction_in_text():
    """The rendered percent is clamped to [0, 100] even for a buggy reporter; the raw
    fraction is preserved in the structured entry for diagnosis."""
    updates: List[Dict[str, Any]] = []
    bridge = ActivityBridge(
        _collect(updates), agent="code_review", label="Code review", min_interval_s=0.0
    )

    bridge("reviewing", "", 1.7)

    assert updates[0]["status_text"] == "Code review (100%)"
    assert updates[0]["current_activity"]["fraction"] == 1.7
