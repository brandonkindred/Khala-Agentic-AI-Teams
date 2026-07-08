"""Unit tests for Task Graph service: add_task, assign, get_task_for_agent, mark_branch_merged."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

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


def test_snapshot_restore_preserves_target_team() -> None:
    """target_team survives persistence so resumed jobs keep v2-team routing."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="UI", target_team="frontend_v2")
    snap = tg.snapshot()

    tg2 = TaskGraphService(job_id="j1")
    tg2.restore(snap)

    assert tg2.get_task("t1").target_team == "frontend_v2"


def test_snapshot_restore_preserves_feature_branch_agent_id() -> None:
    """feature_branch_agent_id survives persistence so a resumed job still pins a task's
    branch to the worktree that actually holds it."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.update_task("t1", feature_branch="feature/t1", feature_branch_agent_id="backend_v2")
    snap = tg.snapshot()

    tg2 = TaskGraphService(job_id="j1")
    tg2.restore(snap)

    assert tg2.get_task("t1").feature_branch_agent_id == "backend_v2"


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


def test_revision_bumps_on_mutation() -> None:
    """revision advances on each mutation so a persister can skip no-op writes."""
    tg = TaskGraphService(job_id="j1")
    assert tg.revision == 0
    tg.add_task("t1", title="T1")
    after_add = tg.revision
    assert after_add > 0
    tg.assign_task_to_agent("t1", "agent-a")
    assert tg.revision > after_add


def test_revision_bumps_on_restore() -> None:
    """A wholesale restore bumps revision so a following persist is not skipped."""
    src = TaskGraphService(job_id="j1")
    src.add_task("t1", title="T1")
    snap = src.snapshot()
    dst = TaskGraphService(job_id="j1")
    before = dst.revision
    dst.restore(snap)
    assert dst.revision > before


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


def test_update_task_clears_assignment_and_frees_agent() -> None:
    """update_task(assigned_agent_id=None) clears the back-reference AND frees the agent mapping."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1")
    tg.assign_task_to_agent("t1", "a1")
    assert tg.get_task_for_agent("a1").id == "t1"

    tg.update_task("t1", status=TaskStatus.TO_DO, assigned_agent_id=None)

    assert tg.get_task("t1").assigned_agent_id is None
    assert tg.get_task_for_agent("a1") is None  # agent freed, not silently left busy (no-op bug)


def test_update_task_omitting_assignment_leaves_it_untouched() -> None:
    """Omitting assigned_agent_id must not clobber an existing assignment (sentinel default)."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1")
    tg.assign_task_to_agent("t1", "a1")

    tg.update_task("t1", feature_branch="feature/t1")  # assigned_agent_id not supplied

    assert tg.get_task("t1").assigned_agent_id == "a1"  # preserved
    assert tg.get_task_for_agent("a1").id == "t1"
    assert tg.get_task("t1").feature_branch == "feature/t1"


def test_count_with_status() -> None:
    """count_with_status tallies tasks in a given status (single source of truth for tallies)."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1")
    tg.add_task("t2")
    tg.add_task("t3")
    tg.mark_branch_merged("t1")
    tg.update_task("t2", status=TaskStatus.FAILED)

    assert tg.count_with_status(TaskStatus.MERGED) == 1
    assert tg.count_with_status(TaskStatus.FAILED) == 1
    assert tg.count_with_status(TaskStatus.TO_DO) == 1
    assert tg.count_with_status(TaskStatus.IN_REVIEW) == 0


def test_reset_failed_demotes_failed_to_todo_and_frees_agent() -> None:
    """reset_failed demotes a terminal FAILED task to TO_DO and releases its agent so a fresh
    swarm can re-pick it (the "retry the failed tasks" action)."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.assign_task_to_agent("t1", "agent-a")
    tg.update_task("t1", status=TaskStatus.FAILED, assigned_agent_id="agent-a")
    assert tg.get_task("t1").status == TaskStatus.FAILED

    tg.reset_failed()

    task = tg.get_task("t1")
    assert task.status == TaskStatus.TO_DO
    assert task.assigned_agent_id is None
    assert tg.get_task_for_agent("agent-a") is None
    assert "agent-a" not in tg.snapshot()["agent_task_map"]


def test_reset_failed_resets_revision_budget_preserving_feedback() -> None:
    """A task that reached FAILED by exhausting the revision cap gets a fresh revision window on
    reset (counters cleared) while its accumulated revision_feedback is preserved as history."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    feedback = [{"source": "tech_lead", "reason": "fix X"}]
    tg.update_task(
        "t1",
        status=TaskStatus.FAILED,
        revision_count=20,  # at MAX_TASK_REVISIONS: without a reset the next bounce re-fails it
        no_change_revisits=3,
        last_change_digest="deadbeef",
        revision_feedback=feedback,
    )

    tg.reset_failed()

    task = tg.get_task("t1")
    assert task.status == TaskStatus.TO_DO
    assert task.revision_count == 0
    assert task.no_change_revisits == 0
    assert task.last_change_digest == ""
    assert task.revision_feedback == feedback  # history preserved


def test_reset_failed_leaves_non_failed_untouched() -> None:
    """reset_failed only touches FAILED tasks; MERGED/TO_DO/IN_PROGRESS are preserved."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="Merged")
    tg.add_task("t2", title="Failed")
    tg.add_task("t3", title="Todo")
    tg.assign_task_to_agent("t1", "agent-a")
    tg.mark_branch_merged("t1")
    tg.update_task("t2", status=TaskStatus.FAILED)

    tg.reset_failed()

    assert tg.get_task("t1").status == TaskStatus.MERGED
    assert tg.get_task("t2").status == TaskStatus.TO_DO
    assert tg.get_task("t3").status == TaskStatus.TO_DO


def test_reset_failed_recovers_cascade_failed_dependents() -> None:
    """A dependent cascade-FAILED via mark_dependents_failed is reset alongside its root, so both
    become eligible again once the (now TO_DO) dependency re-merges."""
    tg = TaskGraphService(job_id="j1")
    tg.add_task("root", title="Root")
    tg.add_task("dep", title="Dependent", dependencies=["root"])
    tg.update_task("root", status=TaskStatus.FAILED)
    newly_failed = tg.mark_dependents_failed("root")
    assert "dep" in newly_failed
    assert tg.get_task("dep").status == TaskStatus.FAILED

    tg.reset_failed()

    assert tg.get_task("root").status == TaskStatus.TO_DO
    assert tg.get_task("dep").status == TaskStatus.TO_DO
    assert tg.count_with_status(TaskStatus.FAILED) == 0


def test_concurrent_assign_to_same_agent_never_double_assigns() -> None:
    """N threads racing assign_task_to_agent for the SAME free agent must leave exactly one
    winner — the lock closes the read-then-write window in assign_task_to_agent (check
    self._agent_to_task, then mutate it) that would otherwise let two threads both observe
    "no current task" and both succeed.
    """
    tg = TaskGraphService(job_id="j1")
    n = 20
    for i in range(n):
        tg.add_task(f"t{i}", title=f"T{i}")
    barrier = threading.Barrier(n)

    def _assign(i: int) -> bool:
        barrier.wait(timeout=10)
        return tg.assign_task_to_agent(f"t{i}", "agent-a")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(_assign, range(n)))

    assert sum(results) == 1
    # The graph itself must be self-consistent: exactly one task IN_PROGRESS and mapped to
    # agent-a; the rest remain untouched TO_DO.
    in_progress = [t for t in tg.get_tasks() if t.status == TaskStatus.IN_PROGRESS]
    assert len(in_progress) == 1
    assert tg.get_task_for_agent("agent-a").id == in_progress[0].id
    assert sum(1 for t in tg.get_tasks() if t.status == TaskStatus.TO_DO) == n - 1


def test_concurrent_update_task_across_distinct_tasks_loses_no_updates() -> None:
    """N threads each mutating a DIFFERENT task concurrently must all land — proves the lock
    serializes dict mutation (self._tasks) without silently dropping a concurrent writer's
    update, which an unsynchronized dict could under contended insert/update patterns.
    """
    tg = TaskGraphService(job_id="j1")
    n = 50
    for i in range(n):
        tg.add_task(f"t{i}", title=f"T{i}")
    barrier = threading.Barrier(n)

    def _update(i: int) -> None:
        barrier.wait(timeout=10)
        tg.update_task(f"t{i}", changes_summary=f"summary-{i}", revision_count=i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(_update, range(n)))

    for i in range(n):
        task = tg.get_task(f"t{i}")
        assert task.changes_summary == f"summary-{i}"
        assert task.revision_count == i


def test_concurrent_mark_branch_merged_and_mark_dependents_failed_stay_consistent() -> None:
    """A cascade-fail racing concurrently with an unrelated task's merge must never corrupt
    the agent->task map or leave an inconsistent status for either task.
    """
    tg = TaskGraphService(job_id="j1")
    tg.add_task("root", title="Root")
    tg.add_task("dep", title="Dependent", dependencies=["root"])
    tg.add_task("unrelated", title="Unrelated")
    tg.assign_task_to_agent("unrelated", "agent-b")
    tg.update_task("root", status=TaskStatus.FAILED)
    barrier = threading.Barrier(2)

    def _cascade() -> None:
        barrier.wait(timeout=10)
        tg.mark_dependents_failed("root")

    def _merge_unrelated() -> None:
        barrier.wait(timeout=10)
        tg.mark_branch_merged("unrelated")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda fn: fn(), [_cascade, _merge_unrelated]))

    assert tg.get_task("dep").status == TaskStatus.FAILED
    assert tg.get_task("unrelated").status == TaskStatus.MERGED
    assert tg.get_task_for_agent("agent-b") is None
