"""Unit tests for Task Graph service: add_task, assign, get_task_for_agent, mark_branch_merged."""

from __future__ import annotations

import pytest

from coding_team.models import TaskStatus
from coding_team.task_graph import TaskGraphService, create_task_graph


def test_add_task() -> None:
    """add_task creates a task with TO_DO status and returns it."""
    tg = TaskGraphService(job_id="j1")
    t = tg.add_task("t1", title="Task 1", description="Do something", dependencies=[])
    assert t.id == "t1"
    assert t.title == "Task 1"
    assert t.status == TaskStatus.TO_DO
    assert tg.get_task("t1") == t
    assert len(tg.get_tasks()) == 1


def test_add_task_duplicate_raises() -> None:
    """add_task with existing id raises ValueError."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="First")
    with pytest.raises(ValueError, match="already exists"):
        tg.add_task("t1", title="Second")


def test_assign_task_to_agent_one_per_agent() -> None:
    """assign_task_to_agent assigns one task per agent; next only after merge."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.add_task("t2", title="T2")
    assert tg.assign_task_to_agent("t1", "agent-a") is True
    assert tg.get_task_for_agent("agent-a").id == "t1"
    # Same agent cannot get another task until current is merged
    assert tg.assign_task_to_agent("t2", "agent-a") is False
    tg.mark_branch_merged("t1")
    assert tg.get_task_for_agent("agent-a") is None
    assert tg.assign_task_to_agent("t2", "agent-a") is True
    assert tg.get_task_for_agent("agent-a").id == "t2"


def test_assign_task_to_agent_deps_satisfied() -> None:
    """assign_task_to_agent allows assignment only when dependencies are merged."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.add_task("t2", title="T2", dependencies=["t1"])
    assert tg.assign_task_to_agent("t2", "agent-a") is False
    assert tg.assign_task_to_agent("t1", "agent-a") is True
    tg.mark_branch_merged("t1")
    assert tg.assign_task_to_agent("t2", "agent-b") is True


def test_get_task_for_agent_returns_none_when_no_assignment() -> None:
    """get_task_for_agent returns None when agent has no active task."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    assert tg.get_task_for_agent("agent-a") is None
    tg.assign_task_to_agent("t1", "agent-a")
    assert tg.get_task_for_agent("agent-a") is not None
    tg.mark_branch_merged("t1")
    assert tg.get_task_for_agent("agent-a") is None


def test_mark_branch_merged() -> None:
    """mark_branch_merged sets task status to MERGED and frees the agent."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.assign_task_to_agent("t1", "agent-a")
    assert tg.get_task("t1").status == TaskStatus.IN_PROGRESS
    assert tg.mark_branch_merged("t1") is True
    assert tg.get_task("t1").status == TaskStatus.MERGED
    assert tg.get_task("t1").merged_at is not None
    assert tg.get_task_for_agent("agent-a") is None


def test_mark_branch_merged_unknown_task_returns_false() -> None:
    """mark_branch_merged returns False for unknown task."""
    tg = TaskGraphService(job_id="j1")
    assert tg.mark_branch_merged("nonexistent") is False


def test_snapshot_restore_roundtrip() -> None:
    """snapshot() and restore() preserve tasks and agent_task_map."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.add_task("t2", title="T2", dependencies=["t1"])
    tg.assign_task_to_agent("t1", "agent-a")
    snap = tg.snapshot()
    tg2 = TaskGraphService(job_id="j1")
    tg2.restore(snap)
    assert len(tg2.get_tasks()) == 2
    assert tg2.get_task_for_agent("agent-a") is not None
    assert tg2.get_task_for_agent("agent-a").id == "t1"


def test_persist_callback_called() -> None:
    """Persist callback is invoked after mutations."""
    calls = []

    def persist() -> None:
        calls.append(1)

    tg = TaskGraphService(job_id="j1", persist_callback=persist)
    tg.add_task("t1", title="T1")
    assert len(calls) == 1
    tg.assign_task_to_agent("t1", "agent-a")
    assert len(calls) == 2


def test_create_task_graph() -> None:
    """create_task_graph returns a TaskGraphService."""
    tg = create_task_graph("job-1")
    assert isinstance(tg, TaskGraphService)
    assert tg.job_id == "job-1"
    tg.add_task("t1", title="T1")
    assert tg.get_task("t1") is not None
