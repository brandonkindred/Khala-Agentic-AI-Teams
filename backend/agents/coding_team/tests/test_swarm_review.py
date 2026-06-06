"""Tests for the coding-team swarm review/merge loop.

Covers the Tech-Lead review deadlock fix: a rejected task is sent back to the SAME engineer
for revision (IN_PROGRESS, assignment retained, reviewer reasons attached) rather than demoted
to TO_DO, and after MAX_TASK_REVISIONS it reaches a terminal FAILED state so the swarm loop can
never spin on it. Also covers full (untruncated) review evidence, summary persistence,
serialization round-trip, the branch_diff helper, and the final status line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from coding_team import orchestrator as orch_mod
from coding_team.models import CodingTeamPlanInput, StackSpec, Task, TaskStatus
from coding_team.orchestrator import CodingTeamSwarm, run_coding_team_orchestrator
from coding_team.senior_software_engineer_agent.agent import (
    SeniorSWEAgent,
    _render_revision_feedback,
)
from coding_team.task_graph import TaskGraphService

GIT_UTILS = "software_engineering_team.shared.git_utils"


# --------------------------------------------------------------------------- stubs


class StubTechLead:
    """Duck-typed Tech Lead: records the review evidence and returns a fixed verdict."""

    def __init__(self, approved: bool, reason: str = "needs work", requested_changes=None) -> None:
        self.approved = approved
        self.reason = reason
        self.requested_changes = requested_changes if requested_changes is not None else ["fix X"]
        self.review_calls: List[str] = []

    def run_code_review(self, task_title, task_description, acceptance_criteria, changes_summary):
        self.review_calls.append(changes_summary)
        return {
            "approved": self.approved,
            "reason": self.reason,
            "requested_changes": self.requested_changes,
        }

    def run_assignments(self, agent_ids, ready_tasks, free_agents):
        assignments = [
            {"agent_id": a, "task_id": t["id"]} for t, a in zip(ready_tasks, free_agents)
        ]
        return {"assignments": assignments}


class StubWorker:
    """Duck-typed Senior SWE that always reports a ready implementation."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.stack_spec = StackSpec(name=agent_id, tools_services=[])
        self.implement_calls: List[Task] = []

    def run_implement(self, task: Task, path, repo_context: str = "") -> Dict[str, Any]:
        self.implement_calls.append(task)
        return {
            "status": "in_review",
            "feature_branch": f"feature/{task.id}",
            "changes_summary": f"did {task.id}",
            "files_to_create_or_edit": [],
            "error": None,
        }


def _make_swarm(tmp_path, tech_lead, workers):
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=workers,
        graph=graph,
        path=Path(tmp_path),
        agent_ids=[w.agent_id for w in workers],
        llm_getter=lambda key: None,
    )
    # Bypass the external quality-gate tools (build/lint/code-review) — not under test here.
    swarm._run_quality_gates = lambda *a, **k: True  # type: ignore[method-assign]
    return swarm, graph


def _patch_git(monkeypatch, diff: str = "", merge=(True, "ok")):
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: diff)
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: merge)


# --------------------------------------------------------------------------- tests


def test_rejected_task_terminates_in_failed(tmp_path, monkeypatch):
    """An always-reject reviewer terminates the loop with the task FAILED and the agent freed."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 2)
    _patch_git(monkeypatch)
    tech_lead = StubTechLead(approved=False)
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=20)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.FAILED
    assert graph.get_task_for_agent("a1") is None  # agent released
    assert len(worker.implement_calls) >= 2  # revised at least once before failing


def test_rejection_routes_back_to_same_engineer(tmp_path, monkeypatch):
    """A rejection keeps the task with its engineer (IN_PROGRESS), not demoted to TO_DO."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 2)
    _patch_git(monkeypatch)
    tech_lead = StubTechLead(
        approved=False, reason="missing tests", requested_changes=["add tests"]
    )
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.IN_PROGRESS  # NOT TaskStatus.TO_DO
    assert task.assigned_agent_id == "a1"  # assignment retained
    assert graph._agent_to_task.get("a1") == "t1"  # mapping retained
    assert task.revision_count == 1
    assert task.revision_feedback[-1]["source"] == "tech_lead"
    assert task.revision_feedback[-1]["reason"] == "missing tests"
    assert task.revision_feedback[-1]["requested_changes"] == ["add tests"]

    # A second rejection exhausts the cap (2) and goes terminal.
    graph.set_task_in_review("t1")
    swarm._review_and_merge(lambda **kw: None)
    assert graph.get_task("t1").status == TaskStatus.FAILED


def test_max_task_revisions_is_20():
    """The shipped per-task revision cap is 20."""
    assert orch_mod.MAX_TASK_REVISIONS == 20


def test_revision_feedback_threaded_into_implement_prompt(tmp_path, monkeypatch):
    """Reviewer reasons on the task reach the SWE's implement prompt so it revises, not restarts."""
    from coding_team.senior_software_engineer_agent import agent as swe_mod

    captured: Dict[str, str] = {}

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def __call__(self, prompt):
            captured["prompt"] = prompt
            return (
                '{"summary":"ok","files_to_create_or_edit":[],"commands_run":[],'
                '"ready_for_review":true,"feature_branch":"feature/t1"}'
            )

    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)
    swe = SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    task = Task(
        id="t1",
        title="T1",
        description="do the thing",
        revision_feedback=[
            {
                "source": "tech_lead",
                "reason": "missing tests",
                "requested_changes": ["add unit tests"],
            }
        ],
    )

    swe.run_implement(task, tmp_path, repo_context="ctx")

    assert "REVISIONS REQUESTED" in captured["prompt"]
    assert "missing tests" in captured["prompt"]
    assert "add unit tests" in captured["prompt"]


def test_render_revision_feedback_formats_entries():
    out = _render_revision_feedback(
        [
            {"source": "tech_lead", "reason": "bad", "requested_changes": ["x", "y"]},
            {"type": "build", "error": "boom"},
        ]
    )
    assert "[tech_lead] bad" in out
    assert "  - x" in out
    assert "  - y" in out
    assert "[build] boom" in out


def test_render_revision_feedback_empty():
    assert _render_revision_feedback([]) == ""


def test_render_revision_feedback_non_dict_entry():
    """Defensive: a non-dict feedback entry is rendered as a plain bullet, not dropped."""
    assert "- just a string" in _render_revision_feedback(["just a string"])


def test_approved_task_merges(tmp_path, monkeypatch):
    _patch_git(monkeypatch, merge=(True, "ok"))
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", feature_branch="feature/t1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.MERGED


def test_approved_merge_failure_marks_merged_anyway(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: "")
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", boom)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.MERGED


def test_full_evidence_reaches_reviewer(tmp_path, monkeypatch):
    """The reviewer gets the real summary + the full diff — no placeholder, no truncation."""
    big_diff = "D" * 20000
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: big_diff)
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: (True, "ok"))
    tech_lead = StubTechLead(approved=False)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", feature_branch="feature/t1", changes_summary="MY REAL SUMMARY")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    evidence = tech_lead.review_calls[0]
    assert "See implementation summary." not in evidence
    assert "MY REAL SUMMARY" in evidence
    assert big_diff in evidence  # full diff passed through, uncut


def test_implement_persists_changes_summary(tmp_path):
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=False), [worker])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")

    swarm._implement_and_verify(worker, lambda **kw: None)

    task = graph.get_task("t1")
    assert task.changes_summary == "did t1"
    assert task.status == TaskStatus.IN_REVIEW


def test_snapshot_restore_preserves_new_fields_and_failed_status():
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.update_task(
        "t1",
        status=TaskStatus.FAILED,
        changes_summary="summary text",
        revision_count=3,
        revision_feedback=[{"source": "tech_lead", "reason": "nope"}],
    )

    tg2 = TaskGraphService(job_id="j1")
    tg2.restore(tg.snapshot())

    task = tg2.get_task("t1")
    assert task.status == TaskStatus.FAILED
    assert task.changes_summary == "summary text"
    assert task.revision_count == 3
    assert task.revision_feedback[0]["reason"] == "nope"


def test_branch_diff_returns_full_diff(tmp_path):
    from software_engineering_team.shared.git_utils import (
        branch_diff,
        create_feature_branch,
        initialize_new_repo,
        write_files_and_commit,
    )

    ok, _ = initialize_new_repo(tmp_path)
    assert ok
    ok, _ = create_feature_branch(tmp_path, "development", "x")
    assert ok
    big = "new line\n" * 5000
    write_files_and_commit(tmp_path, {"b.txt": big}, "add b")

    diff = branch_diff(tmp_path, "development", "feature/x")
    assert "b.txt" in diff
    assert diff.count("+new line") > 1000  # full diff, not truncated


def test_branch_diff_no_repo(tmp_path):
    from software_engineering_team.shared.git_utils import branch_diff

    assert branch_diff(tmp_path / "does-not-exist", "development", "feature/x") == ""


def test_branch_diff_bad_branch_returns_empty(tmp_path):
    """A failing git diff (e.g. unknown branch) yields "" rather than raising."""
    from software_engineering_team.shared.git_utils import branch_diff, initialize_new_repo

    ok, _ = initialize_new_repo(tmp_path)
    assert ok
    assert branch_diff(tmp_path, "development", "feature/does-not-exist") == ""


def test_status_text_reports_merged_and_failed_counts(tmp_path, monkeypatch):
    class StubTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}, {"id": "t2", "title": "T2"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    class StubSWE:
        def __init__(self, *a, **k):
            self.agent_id = k.get("agent_id", "backend")

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")
            self.graph.update_task("t2", status=TaskStatus.FAILED)

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(orch_mod, "SeniorSWEAgent", StubSWE)
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)
    # No job service in unit tests — skip the persistence write.
    monkeypatch.setattr(orch_mod, "update_job_task_graph", lambda *a, **k: None)

    updates: List[Dict[str, Any]] = []
    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    final = updates[-1]
    assert final["status"] == "completed"
    assert "1 merged, 1 failed" in final["status_text"]
