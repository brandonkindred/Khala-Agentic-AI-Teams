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


def test_update_task_to_failed_frees_agent() -> None:
    """update_task(status=FAILED) releases the agent immediately (symmetric with merge).

    Without eager release the mapping lingers until the next get_task_for_agent prunes it, so a
    snapshot persisted right after the failure would still report the agent as occupied.
    """
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.assign_task_to_agent("t1", "agent-a")
    assert "agent-a" in tg.snapshot()["agent_task_map"]
    tg.update_task("t1", status=TaskStatus.FAILED)
    # Mapping gone in the persisted snapshot — not merely lazily pruned on later access.
    assert "agent-a" not in tg.snapshot()["agent_task_map"]
    assert tg.get_task_for_agent("agent-a") is None


def test_update_task_to_failed_keeps_other_agents_mappings() -> None:
    """Failing one task must not disturb a different agent's active mapping."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.add_task("t2", title="T2")
    tg.assign_task_to_agent("t1", "agent-a")
    tg.assign_task_to_agent("t2", "agent-b")
    tg.update_task("t1", status=TaskStatus.FAILED)
    assert tg.get_task_for_agent("agent-a") is None
    assert tg.get_task_for_agent("agent-b") is not None
    assert tg.get_task_for_agent("agent-b").id == "t2"


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


def test_snapshot_restore_round_trips_subtasks() -> None:
    """A task's subtasks survive a snapshot → restore round-trip (serialization + reconstruction)."""
    from coding_team.models import Subtask

    tg = TaskGraphService(job_id="j1")
    tg.add_task(
        "t1",
        title="T1",
        subtasks=[
            Subtask(id="s1", title="S1", description="first", status=TaskStatus.MERGED),
            Subtask(id="s2", title="S2", dependencies=["s1"]),
        ],
    )

    tg2 = TaskGraphService(job_id="j1")
    tg2.restore(tg.snapshot())

    sub = tg2.get_task("t1").subtasks
    assert [s.id for s in sub] == ["s1", "s2"]
    assert sub[0].status == TaskStatus.MERGED
    assert sub[1].dependencies == ["s1"]


def test_get_next_eligible_subtask() -> None:
    """Returns the first subtask whose subtask-deps are all MERGED; None when blocked/none/empty."""
    from coding_team.models import Subtask

    tg = TaskGraphService(job_id="j1")
    assert tg.get_next_eligible_subtask("missing") is None  # unknown task
    tg.add_task("t0", title="no subs")
    assert tg.get_next_eligible_subtask("t0") is None  # task without subtasks
    tg.add_task(
        "t1",
        title="T1",
        subtasks=[
            Subtask(id="s1", title="S1", status=TaskStatus.MERGED),
            Subtask(id="s2", title="S2", dependencies=["s1"]),
            Subtask(id="s3", title="S3", dependencies=["s2"]),  # blocked: s2 not merged
        ],
    )
    nxt = tg.get_next_eligible_subtask("t1")
    assert nxt.id == "s2"  # s1 merged → s2 eligible, s3 still blocked


def test_missing_task_operations_are_safe_noops() -> None:
    """Mutating operations on an unknown task id return falsy/None instead of raising."""
    tg = TaskGraphService(job_id="j1")
    assert tg.update_task("nope", status=TaskStatus.MERGED) is None
    assert tg.mark_branch_merged("nope") is False
    assert tg.set_task_in_review("nope") is False
    assert tg.assign_task_to_agent("nope", "a1") is False


def test_persist_callback_exception_is_swallowed() -> None:
    """A failing persist callback must not break a graph mutation (_maybe_persist guards it)."""

    def boom() -> None:
        raise RuntimeError("persist down")

    tg = TaskGraphService(job_id="j1", persist_callback=boom)
    tg.add_task("t1", title="T1")  # must not raise despite the failing callback
    assert tg.get_task("t1") is not None
