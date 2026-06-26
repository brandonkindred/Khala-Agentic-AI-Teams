"""Tests for the coding-team orchestrator and review/merge loop.

Covers the Tech-Lead review deadlock fix: a rejected task is sent back to the SAME engineer
for revision (IN_PROGRESS, assignment retained, reviewer reasons attached) rather than demoted
to TO_DO, and after MAX_TASK_REVISIONS it reaches a terminal FAILED state so the swarm loop can
never spin on it. Also covers full (untruncated) review evidence, summary persistence,
serialization round-trip, the branch_diff helper, and the final status line.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from coding_team import orchestrator as orch_mod
from coding_team.models import CodingTeamPlanInput, StackSpec, Task, TaskStatus
from coding_team.orchestrator import CodingTeamSwarm, run_coding_team_orchestrator
from coding_team.task_graph import TaskGraphService

GIT_UTILS = "software_engineering_team.shared.git_utils"


# --------------------------------------------------------------------------- stubs


class StubTechLead:
    """Duck-typed Tech Lead: records the review evidence and returns a fixed verdict."""

    def __init__(
        self,
        approved: bool,
        reason: str = "needs work",
        requested_changes=None,
        adjudication_verdict: str = "fail",
    ) -> None:
        self.approved = approved
        self.reason = reason
        self.requested_changes = requested_changes if requested_changes is not None else ["fix X"]
        self.review_calls: List[str] = []
        self.decision_calls: List[Any] = []
        self.adjudication_verdict = adjudication_verdict
        self.adjudication_calls: List[Any] = []

    def run_code_review(
        self,
        task_title,
        task_description,
        acceptance_criteria,
        changes_summary,
        user_decisions=None,
        progress_callback=None,
    ):
        self.review_calls.append(changes_summary)
        self.decision_calls.append(user_decisions)
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

    def run_revision_adjudication(
        self, task_title, task_description, acceptance_criteria, changes_summary, revision_feedback
    ):
        self.adjudication_calls.append(revision_feedback)
        return {"verdict": self.adjudication_verdict, "reason": "stub verdict"}


class StubWorker:
    """Duck-typed implementation worker that always reports a ready implementation."""

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


def test_failed_task_cascades_to_dependents(tmp_path, monkeypatch):
    """When a task is FAILED, tasks depending on it are cascade-FAILED (never satisfiable)."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 1)
    _patch_git(monkeypatch)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=False), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2", dependencies=["t1"])
    graph.add_task("t3", title="T3", dependencies=["t2"])  # transitive dependent
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.FAILED
    assert graph.get_task("t2").status == TaskStatus.FAILED  # direct dependent
    assert graph.get_task("t3").status == TaskStatus.FAILED  # transitive dependent
    assert graph.get_task("t2").revision_feedback[-1]["source"] == "system"


def test_review_error_fails_task_once_without_revision_loop(tmp_path, monkeypatch):
    """A review that errors (e.g. evidence > context) fails the task once — it is not re-reviewed
    every round through the revision loop."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 20)
    _patch_git(monkeypatch)

    class ErrTechLead(StubTechLead):
        def run_code_review(
            self,
            task_title,
            task_description,
            acceptance_criteria,
            changes_summary,
            user_decisions=None,
            progress_callback=None,
        ):
            self.review_calls.append(changes_summary)
            return {
                "approved": False,
                "error": True,
                "reason": "evidence exceeded context",
                "requested_changes": [],
            }

    tech_lead = ErrTechLead(approved=False)
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=20)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.FAILED
    assert len(tech_lead.review_calls) == 1  # reviewed once, not spun through the revision loop
    assert task.revision_feedback[-1]["reason"] == "evidence exceeded context"


def test_swarm_completes_when_dependency_fails(tmp_path, monkeypatch):
    """The loop terminates (does not spin to max_rounds) when a dependency fails review."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 2)
    _patch_git(monkeypatch)
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=False), [worker])
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2", dependencies=["t1"])

    swarm.run(max_rounds=50)

    assert graph.get_task("t1").status == TaskStatus.FAILED
    assert graph.get_task("t2").status == TaskStatus.FAILED
    assert graph.get_task("t2") not in worker.implement_calls  # dependent never implemented
    assert swarm._is_complete()


# ----------------------------------------------------- review retry / failure handling


def test_review_retries_transient_error_then_succeeds(monkeypatch):
    """A transient reviewer error (rate limit/timeout) is retried, not turned into a rejection."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def flaky(agent, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return {"approved": True, "reason": "ok", "requested_changes": []}

    monkeypatch.setattr(tl_mod, "_agent_call_json", flaky)
    tl = tl_mod.TechLeadAgent(model=object())

    out = tl.run_code_review("t", "d", [], "evidence")

    assert out["approved"] is True
    assert out["error"] is False
    assert calls["n"] == 2  # retried once after the transient failure


def test_review_returns_error_after_exhausting_retries(monkeypatch):
    """After all attempts fail, the verdict is flagged error=True (not a substantive rejection)."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "1")  # → 2 attempts
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)

    def boom(agent, prompt):
        raise RuntimeError("context overflow")

    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    tl = tl_mod.TechLeadAgent(model=object())

    out = tl.run_code_review("t", "d", [], "x")

    assert out["error"] is True
    assert out["approved"] is False
    assert "2 attempts" in out["reason"]


def test_review_missing_approved_is_infra_error_not_rejection(monkeypatch):
    """A parseable verdict with no 'approved' field must surface error=True (fail once), not be
    silently coerced to approved=False and re-sent through the revision loop."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "1")  # → 2 attempts
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    # Valid JSON, but no verdict — e.g. a weak/over-context model that omits 'approved'.
    monkeypatch.setattr(tl_mod, "_agent_call_json", lambda agent, prompt: {"reason": "hmm"})
    tl = tl_mod.TechLeadAgent(model=object())

    out = tl.run_code_review("t", "d", [], "evidence")

    assert out["error"] is True
    assert out["approved"] is False


def test_review_explicit_false_is_substantive_rejection(monkeypatch):
    """An explicit approved=False is a real rejection (error=False), not an infra failure."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt: {"approved": False, "reason": "needs tests"},
    )
    tl = tl_mod.TechLeadAgent(model=object())

    out = tl.run_code_review("t", "d", [], "evidence")

    assert out["error"] is False
    assert out["approved"] is False
    assert out["reason"] == "needs tests"


def test_review_retry_attempts_env_parsing(monkeypatch):
    """_review_retry_attempts: valid → retries+1; negative/garbage/empty → documented default."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "3")
    assert tl_mod._review_retry_attempts() == 4  # 3 retries + 1
    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "0")
    assert tl_mod._review_retry_attempts() == 1  # 0 retries → single attempt
    # A negative value must restore the default (3 attempts), not collapse to a single attempt
    # that strips all transient-failure protection.
    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "-1")
    assert tl_mod._review_retry_attempts() == 3
    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "-99")
    assert tl_mod._review_retry_attempts() == 3
    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "garbage")
    assert tl_mod._review_retry_attempts() == 3
    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "   ")
    assert tl_mod._review_retry_attempts() == 3
    monkeypatch.delenv("CODING_TEAM_REVIEW_RETRIES", raising=False)
    assert tl_mod._review_retry_attempts() == 3


def test_quality_gate_failure_appends_to_accumulated_feedback(tmp_path, monkeypatch):
    """A quality-gate revision must not clobber prior (e.g. Tech Lead) feedback."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 5)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=False), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", revision_feedback=[{"source": "tech_lead", "reason": "add tests"}])

    swarm._return_for_revision(graph.get_task("t1"), [{"type": "build", "error": "boom"}])

    fb = graph.get_task("t1").revision_feedback
    assert fb[0]["source"] == "tech_lead"  # prior feedback preserved
    assert fb[-1]["type"] == "build"  # gate feedback appended


def test_persistent_implement_failure_fails_task(tmp_path, monkeypatch):
    """A run_implement that keeps failing is bounded by the revision cap, not spun to max_rounds."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 2)
    _patch_git(monkeypatch)

    class FailingWorker(StubWorker):
        def run_implement(self, task, path, repo_context=""):
            self.implement_calls.append(task)
            return {
                "status": "failed",
                "feature_branch": f"feature/{task.id}",
                "changes_summary": "",
                "error": "llm exploded",
            }

    worker = FailingWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=50)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.FAILED
    assert task.revision_feedback[-1]["source"] == "engineer"
    assert len(worker.implement_calls) <= orch_mod.MAX_TASK_REVISIONS + 1  # bounded, not 50


def test_approved_task_merges(tmp_path, monkeypatch):
    """Approved in-review tasks are merged and marked terminal."""
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
    """The reviewer gets the real summary + the full diff — no placeholder, no truncation.

    The diff is deliberately larger than any cap the team ever used, proving the evidence is
    passed through whole.
    """
    big_diff = "D" * 120000
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

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")
            self.graph.update_task("t2", status=TaskStatus.FAILED)

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)
    # No job service in unit tests — skip the persistence write.

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
    assert final["status"] == "completed_with_failures"  # a failed task is not a clean success
    assert "1 merged, 1 failed" in final["status_text"]


# ----------------------------------------------------- real quality-gate path

QG = "software_engineering_team.quality_gate_tools"


def _make_real_swarm(tmp_path):
    """A swarm WITHOUT the _run_quality_gates bypass, with one task already assigned to a1."""
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
    )
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    return swarm, graph


def _patch_gates(monkeypatch, *, build_ok=True, review_ok=True, build_raises=False):
    import types

    def build(*a, **k):
        if build_raises:
            raise RuntimeError("tool crashed")
        return types.SimpleNamespace(success=build_ok, error="" if build_ok else "boom build")

    monkeypatch.setattr(f"{QG}.run_build_verification", build)
    monkeypatch.setattr(f"{QG}.run_linting", lambda *a, **k: None)
    monkeypatch.setattr(
        f"{QG}.run_code_review",
        lambda **k: types.SimpleNamespace(
            approved=review_ok, issues=[] if review_ok else [{"type": "review", "error": "x"}]
        ),
    )


def test_quality_gates_pass_sets_in_review(tmp_path, monkeypatch):
    _patch_gates(monkeypatch, build_ok=True, review_ok=True)
    swarm, graph = _make_real_swarm(tmp_path)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


def test_quality_gate_build_failure_returns_for_revision(tmp_path, monkeypatch):
    _patch_gates(monkeypatch, build_ok=False)
    swarm, graph = _make_real_swarm(tmp_path)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO  # returned for revision
    assert task.assigned_agent_id is None  # and unassigned


def test_quality_gate_review_rejection_returns_for_revision(tmp_path, monkeypatch):
    _patch_gates(monkeypatch, build_ok=True, review_ok=False)
    swarm, graph = _make_real_swarm(tmp_path)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.TO_DO


def test_quality_gate_tool_exception_proceeds_to_review(tmp_path, monkeypatch):
    """An unexpected quality-gate tool error is logged and the task still proceeds (best-effort)."""
    _patch_gates(monkeypatch, build_raises=True)
    swarm, graph = _make_real_swarm(tmp_path)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW  # proceeded despite the tool error


def test_quality_gate_tool_exception_logs_full_traceback(tmp_path, monkeypatch, caplog):
    """An unexpected quality-gate tool error must be logged WITH a full traceback
    (logger.exception → ERROR + exc_info), not a one-line WARNING — a silent
    summary is exactly what made the review-phase crash undebuggable."""
    import logging as _logging

    _patch_gates(monkeypatch, build_raises=True)  # build() raises RuntimeError("tool crashed")
    swarm, graph = _make_real_swarm(tmp_path)

    with caplog.at_level(_logging.ERROR, logger="coding_team.orchestrator"):
        swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    gate_errors = [r for r in caplog.records if "Quality gate tools error" in r.getMessage()]
    assert gate_errors, "the quality-gate tool error must be logged"
    record = gate_errors[0]
    assert record.levelno == _logging.ERROR  # logger.exception logs at ERROR
    # Inspect the attached exception directly rather than rely on log formatting:
    # the full traceback must be carried so the cause is debuggable.
    assert record.exc_info is not None
    exc = record.exc_info[1]
    assert isinstance(exc, RuntimeError) and exc.args[0] == "tool crashed"


def test_code_review_runs_even_if_progress_bridge_fails(tmp_path, monkeypatch):
    """A failure constructing the ActivityBridge (observability only) must NOT skip
    the code review and silently pass the gate. The review still runs: a rejecting
    review returns the task for revision (TO_DO), not IN_REVIEW."""

    def _boom(*_a, **_k):
        raise RuntimeError("bridge down")

    _patch_gates(monkeypatch, build_ok=True, review_ok=False)  # review rejects
    monkeypatch.setattr(orch_mod, "ActivityBridge", _boom)
    swarm, graph = _make_real_swarm(tmp_path)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    # Review ran (and rejected) despite the bridge failure → returned for revision.
    # Before the fix the bridge error was swallowed and the gate passed (IN_REVIEW).
    assert graph.get_task("t1").status == TaskStatus.TO_DO


# ----------------------------------------------------- un-assignment / double-assignment guard


def test_return_for_revision_unassigns_task(tmp_path, monkeypatch):
    """A quality-gate rejection must genuinely unassign the task (TO_DO + agent freed), not leave it
    mapped to its agent — otherwise it can be both re-served to that agent and assigned to a second
    free agent next round (two workers on one task)."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 5)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=False), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")  # IN_PROGRESS, mapped to a1

    ready = swarm._return_for_revision(graph.get_task("t1"), [{"type": "build", "error": "boom"}])

    assert ready is False
    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO
    assert task.assigned_agent_id is None  # genuinely unassigned (was a silent no-op before)
    assert graph.get_task_for_agent("a1") is None  # agent freed
    assert graph._agent_to_task.get("a1") is None  # mapping cleared
    # It can now be cleanly reassigned (no "already has active task" rejection).
    assert graph.assign_task_to_agent("t1", "a1") is True


def test_assignment_respects_target_team_and_falls_back_to_matching_v2_worker(tmp_path):
    """A mismatched LLM assignment is ignored; target_team routes to the matching v2 worker."""

    class MismatchingTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": [{"agent_id": "backend_v2", "task_id": "ui"}]}

    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, MismatchingTL(approved=True), workers)
    graph.add_task("ui", title="Build UI", target_team="frontend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    task = graph.get_task("ui")
    assert task.assigned_agent_id == "frontend_v2"
    assert graph.get_task_for_agent("backend_v2") is None


def test_assignment_normalizes_backend_owned_target_aliases(tmp_path):
    """Backend-owned target aliases such as devops route to the backend v2 worker."""

    class AssignDevOpsTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": [{"agent_id": "backend_v2", "task_id": "deploy"}]}

    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, AssignDevOpsTL(approved=True), workers)
    graph.add_task("deploy", title="Deploy service", target_team="devops")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    task = graph.get_task("deploy")
    assert task.assigned_agent_id == "backend_v2"
    assert graph.get_task_for_agent("frontend_v2") is None


def test_assignment_fails_task_with_unrecognized_target_team(tmp_path, caplog):
    """A target_team that no worker can satisfy fails instead of waiting forever."""

    class NoopTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": []}

    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, NoopTL(approved=True), workers)
    graph.add_task("unknown", title="Unknown target", target_team="unknown_team")

    with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
        swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    task = graph.get_task("unknown")
    assert task.status == TaskStatus.FAILED
    assert task.assigned_agent_id is None
    assert "No implementation worker is available" in task.changes_summary
    assert task.revision_feedback[-1]["source"] == "system"
    assert "unknown_team" in caplog.text


def test_target_match_normalizes_raw_v2_agent_ids() -> None:
    """Raw worker IDs with suffixes still compare by their canonical v2 team."""
    assert orch_mod._target_matches_agent("frontend_v2", "frontend-v2-worker-2") is True
    assert orch_mod._target_matches_agent("devops", "backend_v2_worker_1") is True
    assert orch_mod._target_matches_agent("frontend_v2", "backend_v2_worker_1") is False


def test_team_key_warns_on_ambiguous_frontend_backend_label(caplog) -> None:
    """Ambiguous raw labels are visible in logs while preserving current precedence."""
    with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
        assert orch_mod._team_key("frontend-backend") == "frontend_v2"

    assert "contains both frontend and backend" in caplog.text


def test_quality_gate_type_uses_v2_stack_inference_for_hint_stack_names() -> None:
    """Hint-only stack names still map to canonical quality gate agent types."""
    assert orch_mod._quality_gate_agent_type("Angular") == "frontend"
    assert orch_mod._quality_gate_agent_type("Spring Boot") == "backend"
    assert orch_mod._quality_gate_agent_type("Python") == "backend"


def test_v2_team_kind_matches_frontend_hint_tokens_without_substrings() -> None:
    """Frontend hint aliases must match as tokens, not substrings inside unrelated words."""
    assert orch_mod._v2_team_kind_for_stack(StackSpec(name="UI", tools_services=[])) == "frontend"
    assert orch_mod._v2_team_kind_for_stack(StackSpec(name="build", tools_services=[])) == "backend"
    assert (
        orch_mod._v2_team_kind_for_stack(
            StackSpec(name="documentation", tools_services=["guides"])
        )
        is None
    )
    assert (
        orch_mod._v2_team_kind_for_stack(
            StackSpec(name="release automation", tools_services=["CI build"])
        )
        == "backend"
    )


@pytest.mark.parametrize("stack_name", ["platform", "ci_cd", "services"])
def test_v2_team_kind_accepts_backend_alias_stack_names(stack_name: str) -> None:
    """Backend-owned alias stack names build backend v2 workers instead of failing."""
    assert orch_mod._v2_team_kind_for_stack(StackSpec(name=stack_name, tools_services=[])) == "backend"


@pytest.mark.parametrize("stack_name", ["default", "Senior Software Engineer"])
def test_v2_team_kind_accepts_legacy_default_stack_names(stack_name: str) -> None:
    """Legacy generic stack names now route to backend v2 after removing the Senior SWE worker."""
    assert orch_mod._v2_team_kind_for_stack(StackSpec(name=stack_name, tools_services=[])) == "backend"


def test_legacy_default_stack_spec_is_repaired_to_backend_v2() -> None:
    """Persisted pre-v2 fallback stacks are replaced with the backend v2 team."""
    stacks = orch_mod._ensure_target_team_stack_specs(
        [{"name": "default", "tools_services": ["legacy"]}],
        [],
    )

    assert stacks == [
        {
            "name": "backend_v2",
            "tools_services": ["Java", "Python", "Node.js", "Databases", "APIs", "DevOps"],
        }
    ]


def test_resume_with_legacy_default_stack_builds_backend_v2_worker(tmp_path, monkeypatch):
    """Old persisted jobs with a default stack still resume after the legacy worker removal."""

    class ExplodingTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            raise AssertionError("planning must not run on resume")

    captured_specs: List[str] = []

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            pass

    def _build_worker(agent_id, spec, llm_getter):
        captured_specs.append(spec.name)
        return StubWorker(agent_id)

    monkeypatch.setattr(orch_mod, "TechLeadAgent", ExplodingTL)
    monkeypatch.setattr(orch_mod, "_build_implementation_worker", _build_worker)
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    snapshot = {
        "task_graph_snapshot": [
            {"id": "t1", "title": "T1", "status": "to_do", "dependencies": []},
        ],
        "stack_specs": [{"name": "default", "tools_services": ["legacy"]}],
    }
    updates: List[Dict[str, Any]] = []
    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: snapshot,
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert captured_specs == ["backend_v2"]
    assert any(update.get("stack_specs") == [orch_mod._BACKEND_V2_STACK_SPEC] for update in updates)


def test_backend_v2_worker_uses_injected_llm_getter(monkeypatch):
    """Backend v2 worker construction must honor the coding-team LLM injection path."""
    from software_engineering_team import backend_code_v2_team

    captured_keys: List[str] = []

    class FakeBackendLead:
        def __init__(self, llm):
            self.llm = llm

    monkeypatch.setattr(backend_code_v2_team, "BackendCodeV2TeamLead", FakeBackendLead)

    def _llm_getter(key: str) -> str:
        captured_keys.append(key)
        return f"{key}-client"

    worker = orch_mod._build_implementation_worker(
        "backend_v2",
        StackSpec(name="backend_v2", tools_services=["Python"]),
        _llm_getter,
    )

    assert captured_keys == ["backend"]
    assert worker.team_lead.llm == "backend-client"


def test_frontend_v2_worker_uses_injected_llm_getter(monkeypatch):
    """Frontend v2 worker construction must honor the coding-team LLM injection path."""
    from software_engineering_team import frontend_code_v2_team

    captured_keys: List[str] = []

    class FakeFrontendLead:
        def __init__(self, llm):
            self.llm = llm

    monkeypatch.setattr(frontend_code_v2_team, "FrontendCodeV2TeamLead", FakeFrontendLead)

    def _llm_getter(key: str) -> str:
        captured_keys.append(key)
        return f"{key}-client"

    worker = orch_mod._build_implementation_worker(
        "frontend_v2",
        StackSpec(name="frontend_v2", tools_services=["React"]),
        _llm_getter,
    )

    assert captured_keys == ["frontend"]
    assert worker.team_lead.llm == "frontend-client"


def test_target_team_alias_adds_missing_backend_v2_stack_spec() -> None:
    """Backend-owned aliases repair an incomplete stack roster before worker creation."""
    graph = TaskGraphService(job_id="j1")
    graph.add_task("deploy", title="Deploy service", target_team="infrastructure")

    stacks = orch_mod._ensure_target_team_stack_specs([], graph.get_tasks())

    assert {
        "name": "backend_v2",
        "tools_services": ["Java", "Python", "Node.js", "Databases", "APIs", "DevOps"],
    } in stacks


def test_target_team_adds_missing_v2_stack_specs() -> None:
    """Targeted v2 tasks repair an incomplete stack roster before worker creation."""
    graph = TaskGraphService(job_id="j1")
    graph.add_task("ui", title="Build UI", target_team="frontend_v2")
    graph.add_task("api", title="Build API", target_team="backend_v2")

    stacks = orch_mod._ensure_target_team_stack_specs(
        [{"name": "backend", "tools_services": []}],
        graph.get_tasks(),
    )

    assert {
        "name": "frontend_v2",
        "tools_services": ["Angular", "TypeScript", "React", "CSS", "HTML"],
    } in stacks
    assert {"name": "backend", "tools_services": []} in stacks
    assert [s.get("name") for s in stacks].count("backend_v2") == 0


# ----------------------------------------------------- IN_REVIEW is not re-implemented


def test_in_review_task_is_not_reimplemented(tmp_path):
    """A worker whose mapped task is IN_REVIEW (awaiting Tech Lead review) must not re-run implement
    — that would regenerate code under review and churn the loop."""
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")  # IN_REVIEW, still mapped to a1

    swarm._implement_and_verify(worker, lambda **kw: None)

    assert worker.implement_calls == []  # not re-implemented while under review
    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


# ----------------------------------------------------- repo context visibility / refresh


def test_read_repo_context_includes_markdown(tmp_path):
    """Markdown docs must be visible in repo context so a docs task does not see an 'empty' repo."""
    (tmp_path / "spec.md").write_text("# Spec\nThe plan lives here.")
    ctx = orch_mod._read_repo_context(tmp_path)
    assert "spec.md" in ctx
    assert "The plan lives here." in ctx


def test_read_repo_context_is_not_truncated(tmp_path):
    """The briefing renders each included file in full and never drops a file to fit a size budget.

    Guards against reintroducing the old per-file 500-char slice and the 4000-char total budget:
    the engineer's repo context is an LLM input and is never truncated. The 80-file scan ceiling is
    a deliberate cap, not truncation, so this stays within it.
    """
    # A single file well past the old 500-char per-file slice — its tail must survive.
    big_tail = "TAIL_MARKER_" + ("Z" * 4000)
    (tmp_path / "big.py").write_text("# header\n" + big_tail)

    # Several files whose combined size blows past the old 4000-char budget; a late file (by sorted
    # order) must still appear rather than being dropped once the budget filled.
    for i in range(10):
        (tmp_path / f"mod_{i:02d}.py").write_text(f"VALUE_{i:02d} = " + "'" + ("x" * 1000) + "'\n")

    ctx = orch_mod._read_repo_context(tmp_path)

    assert big_tail in ctx  # full file contents, not a 500-char prefix
    assert len(ctx) > 4000  # no total-size budget cut the briefing short
    assert "mod_09.py" in ctx  # the last file survived; no file dropped to fit a budget
    assert "VALUE_09" in ctx


def test_read_repo_context_prunes_excluded_dirs(tmp_path):
    """Files under excluded dirs (node_modules/.git) must not be walked or included,
    and must not consume the 80-file budget ahead of real source files."""
    (tmp_path / "real.py").write_text("REAL_SOURCE = 1")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.py").write_text("VENDORED = 1")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config.py").write_text("GIT_INTERNAL = 1")

    ctx = orch_mod._read_repo_context(tmp_path)

    assert "real.py" in ctx
    assert "REAL_SOURCE" in ctx
    assert "VENDORED" not in ctx
    assert "GIT_INTERNAL" not in ctx


def test_read_repo_context_skips_special_files_without_hanging(tmp_path):
    """A FIFO/special file with a code suffix must be skipped via is_file(), not read
    — read_text() on a FIFO blocks forever (a hang the try/except cannot catch)."""
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo not available on this platform")
    (tmp_path / "real.py").write_text("REAL = 1")
    os.mkfifo(str(tmp_path / "pipe.py"))  # code-suffixed special file

    # If the is_file() guard regressed, this call would block forever on the FIFO.
    ctx = orch_mod._read_repo_context(tmp_path)

    assert "REAL = 1" in ctx
    assert "pipe.py" not in ctx


def test_repo_context_refreshed_between_rounds(tmp_path, monkeypatch):
    """The swarm re-reads repo context each round so files written in earlier rounds become visible
    to later implementations (instead of being recreated)."""
    _patch_git(monkeypatch)
    seen: List[str] = []

    class ContextWorker(StubWorker):
        def run_implement(self, task, path, repo_context=""):
            seen.append(repo_context)
            (Path(path) / "notes.md").write_text("notes round content")
            return super().run_implement(task, path, repo_context=repo_context)

    worker = ContextWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2", dependencies=["t1"])

    swarm.run(max_rounds=20)

    # notes.md (written while implementing t1) is picked up by a later round's context refresh.
    assert any("notes.md" in ctx for ctx in seen)


# ----------------------------------------------------- resume from snapshot (no re-planning)


def test_resume_from_snapshot_skips_planning(tmp_path, monkeypatch):
    """A retry with a persisted task snapshot restores the graph and does NOT re-run planning;
    MERGED tasks are preserved and in-flight tasks are reset to unassigned TO_DO."""

    class ExplodingTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            raise AssertionError("planning must not run on resume")

    captured: Dict[str, Any] = {}

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            captured["graph"] = self.graph

        def run(self, **kw):
            pass  # leave the restored state as-is so we can assert on it

    monkeypatch.setattr(orch_mod, "TechLeadAgent", ExplodingTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    snapshot = {
        "task_graph_snapshot": [
            {"id": "t1", "title": "T1", "status": "merged", "dependencies": []},
            {
                "id": "t2",
                "title": "T2",
                "status": "in_progress",
                "dependencies": [],
                "assigned_agent_id": "backend",
            },
        ],
        "agent_task_map": {"backend": "t2"},
        "stack_specs": [{"name": "backend", "tools_services": []}],
    }
    updates: List[Dict[str, Any]] = []
    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: snapshot,
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    graph = captured["graph"]
    assert graph.get_task("t1").status == TaskStatus.MERGED  # finished work preserved
    assert graph.get_task("t2").status == TaskStatus.TO_DO  # in-flight reset
    assert graph.get_task("t2").assigned_agent_id is None  # and unassigned
    # No stack_specs persisted on the resume path (stacks came from the snapshot, not planning).
    assert not any("stack_specs" in u for u in updates)
    assert updates[-1]["status"] == "completed"  # 1 merged, 0 failed


def test_fresh_run_persists_stack_specs(tmp_path, monkeypatch):
    """The fresh (non-resume) path persists the stacks so a later retry can resume without
    re-planning."""

    class StubTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": ["pytest"]}],
            }

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []
    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {},  # no snapshot → fresh path
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    stack_updates = [u for u in updates if "stack_specs" in u]
    assert stack_updates, "fresh run must persist stack_specs for resume"
    assert stack_updates[0]["stack_specs"] == [{"name": "backend", "tools_services": ["pytest"]}]


# ----------------------------------------------------- task graph helpers (direct)


def test_unassign_task_frees_agent_and_clears_assignment():
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1")
    tg.assign_task_to_agent("t1", "a1")

    tg.unassign_task("t1")

    assert tg.get_task("t1").assigned_agent_id is None
    assert tg.get_task_for_agent("a1") is None
    tg.unassign_task("missing")  # unknown id is a no-op, must not raise


def test_reset_in_flight_demotes_only_nonterminal():
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t_inprog")
    tg.assign_task_to_agent("t_inprog", "a1")
    tg.add_task("t_review")
    tg.assign_task_to_agent("t_review", "a2")
    tg.set_task_in_review("t_review")
    tg.add_task("t_merged")
    tg.mark_branch_merged("t_merged")
    tg.add_task("t_failed")
    tg.update_task("t_failed", status=TaskStatus.FAILED)

    tg.reset_in_flight()

    assert tg.get_task("t_inprog").status == TaskStatus.TO_DO
    assert tg.get_task("t_inprog").assigned_agent_id is None
    assert tg.get_task("t_review").status == TaskStatus.TO_DO
    assert tg.get_task("t_merged").status == TaskStatus.MERGED  # terminal untouched
    assert tg.get_task("t_failed").status == TaskStatus.FAILED  # terminal untouched
    assert tg._agent_to_task == {}


def test_status_is_completed_when_no_failures(tmp_path, monkeypatch):
    class StubTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

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

    assert updates[-1]["status"] == "completed"
    assert "1 merged, 0 failed" in updates[-1]["status_text"]


# ----------------------------------------------------- in_progress (not-ready) is bounded


def test_in_progress_implementation_is_bounded(tmp_path, monkeypatch):
    """A worker that never marks ready_for_review (status='in_progress') is bounded by the revision
    cap and ends FAILED — not spinning to max_rounds and then reported as a clean success."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 3)
    _patch_git(monkeypatch)

    class NeverReadyWorker(StubWorker):
        def run_implement(self, task, path, repo_context=""):
            self.implement_calls.append(task)
            return {
                "status": "in_progress",  # model set ready_for_review=false
                "feature_branch": f"feature/{task.id}",
                "changes_summary": "",
                "error": None,
            }

    worker = NeverReadyWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=50)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.FAILED  # bounded, not stuck IN_PROGRESS forever
    assert task.revision_feedback[-1]["source"] == "engineer"
    assert "ready for review" in task.revision_feedback[-1]["reason"].lower()
    assert len(worker.implement_calls) <= orch_mod.MAX_TASK_REVISIONS + 1  # bounded, not 50
    assert swarm._is_complete()  # loop terminates


# ------------------------------------------ no-change revisit cap → Tech Lead adjudication


def test_no_change_revisit_cap_env_parsing(monkeypatch):
    """Cap parses defensively: garbage → default, floored at 1, never disabled."""
    monkeypatch.delenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", raising=False)
    assert orch_mod._no_change_revisit_cap() == orch_mod.NO_CHANGE_REVISIT_CAP
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "7")
    assert orch_mod._no_change_revisit_cap() == 7
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "garbage")
    assert orch_mod._no_change_revisit_cap() == orch_mod.NO_CHANGE_REVISIT_CAP
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "0")
    assert orch_mod._no_change_revisit_cap() == 1  # floored, guard can never be disabled


def test_note_revision_progress_counts_no_change_then_resets(tmp_path, monkeypatch):
    """Identical diffs across rounds accrue no_change_revisits; a changed diff resets it."""
    diffs = iter(["", "", "CHANGED", "CHANGED"])
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: next(diffs))
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "3")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=False), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    task = graph.get_task("t1")

    assert swarm._note_revision_progress(task) is False  # "" baseline → no_change 0
    assert task.no_change_revisits == 0
    assert swarm._note_revision_progress(task) is False  # "" again → no_change 1
    assert task.no_change_revisits == 1
    assert swarm._note_revision_progress(task) is False  # "CHANGED" → progress, reset
    assert task.no_change_revisits == 0
    assert swarm._note_revision_progress(task) is False  # "CHANGED" again → no_change 1


def test_escalate_done_marks_resolved_without_changes(tmp_path, monkeypatch):
    # _patch_git defaults to an EMPTY branch diff → genuinely nothing landed → resolved-without-changes.
    _patch_git(monkeypatch)
    tech_lead = StubTechLead(approved=False, adjudication_verdict="done")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")

    swarm._escalate_to_tech_lead(graph.get_task("t1"))

    task = graph.get_task("t1")
    assert task.status == TaskStatus.MERGED
    assert task.resolved_without_changes is True
    assert graph.get_task_for_agent("a1") is None  # agent freed
    assert tech_lead.adjudication_calls  # the history was handed to the Tech Lead


def test_escalate_done_non_empty_branch_merges_and_preserves_work(tmp_path, monkeypatch):
    """A 'done' verdict on a stalled but NON-empty branch merges the work (it is not a no-op), so the
    real changes reach development and the PR — instead of being silently dropped and mis-reported as
    an already-complete resolution."""
    merge_calls: List[Any] = []
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: "real unmerged changes")
    monkeypatch.setattr(
        f"{GIT_UTILS}.merge_branch", lambda *a, **k: (merge_calls.append(a) or (True, "ok"))
    )
    tech_lead = StubTechLead(approved=False, adjudication_verdict="done")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", feature_branch="feature/t1")

    swarm._escalate_to_tech_lead(graph.get_task("t1"))

    task = graph.get_task("t1")
    assert task.status == TaskStatus.MERGED
    assert task.resolved_without_changes is False  # real work landed → not a no-op resolution
    assert merge_calls  # the branch was actually merged into development
    assert graph.get_task_for_agent("a1") is None  # agent freed


def test_escalate_done_failed_merge_marks_failed_not_merged(tmp_path, monkeypatch):
    """A 'done' verdict whose non-empty branch fails to merge (conflict/checkout failure) must FAIL
    the task and cascade — not mark it merged, which would drop the work and could leave a conflicted
    tree. It must also abort the half-applied merge so later tasks/publish run on a clean checkout."""
    aborted: list[Any] = []
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: "real unmerged changes")
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: (False, "merge conflict"))
    monkeypatch.setattr(
        f"{GIT_UTILS}.abort_merge", lambda p, *a, **k: (aborted.append(p) or (True, "aborted"))
    )
    swarm, graph = _make_swarm(
        tmp_path, StubTechLead(approved=False, adjudication_verdict="done"), [StubWorker("a1")]
    )
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2", dependencies=["t1"])
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", feature_branch="feature/t1")

    swarm._escalate_to_tech_lead(graph.get_task("t1"))

    assert graph.get_task("t1").status == TaskStatus.FAILED  # not merged
    assert graph.get_task("t1").resolved_without_changes is False
    assert graph.get_task("t2").status == TaskStatus.FAILED  # dependent cascade-failed
    assert aborted  # the conflicted merge was aborted before cascading


def test_escalate_fail_marks_failed_and_cascades(tmp_path, monkeypatch):
    _patch_git(monkeypatch)
    swarm, graph = _make_swarm(
        tmp_path, StubTechLead(approved=False, adjudication_verdict="fail"), [StubWorker("a1")]
    )
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2", dependencies=["t1"])
    graph.assign_task_to_agent("t1", "a1")

    swarm._escalate_to_tech_lead(graph.get_task("t1"))

    assert graph.get_task("t1").status == TaskStatus.FAILED
    assert graph.get_task("t2").status == TaskStatus.FAILED  # dependent cascade-failed


def test_escalate_continue_resets_window(tmp_path, monkeypatch):
    _patch_git(monkeypatch)
    swarm, graph = _make_swarm(
        tmp_path, StubTechLead(approved=False, adjudication_verdict="continue"), [StubWorker("a1")]
    )
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", no_change_revisits=5)

    graph.update_task("t1", last_change_digest="seeded-digest")

    swarm._escalate_to_tech_lead(graph.get_task("t1"))

    task = graph.get_task("t1")
    assert task.status == TaskStatus.IN_PROGRESS  # back to the engineer
    assert task.no_change_revisits == 0  # fresh window
    assert task.last_change_digest == ""  # digest cleared so the next round is a fresh baseline


def test_escalate_continue_clears_digest_so_cap_one_grants_a_real_window(tmp_path, monkeypatch):
    """Regression: with CAP=1 a 'continue' must clear last_change_digest, or the next unchanged round
    re-trips the cap immediately (the escalation bounce does not bump revision_count, so the churn is
    not bounded by MAX_TASK_REVISIONS) and the task never gets a real revision window."""
    import hashlib

    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "1")
    _patch_git(monkeypatch)  # constant empty diff → the branch stays unchanged across rounds
    swarm, graph = _make_swarm(
        tmp_path, StubTechLead(approved=False, adjudication_verdict="continue"), [StubWorker("a1")]
    )
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    # Seed a digest matching the (empty) branch so the task is mid-no-change-loop.
    graph.update_task(
        "t1", last_change_digest=hashlib.sha256(b"").hexdigest(), no_change_revisits=1
    )

    swarm._escalate_to_tech_lead(graph.get_task("t1"))
    assert graph.get_task("t1").last_change_digest == ""  # cleared for a fresh baseline

    # The very next no-change check is now a fresh baseline (no_change=0), NOT an immediate
    # re-escalation — the engineer actually gets a round to change the code.
    escalated = swarm._note_revision_progress(graph.get_task("t1"))
    assert escalated is False
    assert graph.get_task("t1").no_change_revisits == 0


def test_return_for_revision_escalates_on_no_change(tmp_path, monkeypatch):
    """A quality-gate rejection with no diff change escalates to the Tech Lead and returns False."""
    import hashlib

    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: "")
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: (True, "ok"))
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "1")
    tech_lead = StubTechLead(approved=False, adjudication_verdict="done")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    # Seed the prior digest so this round registers as a no-change repeat (cap=1 → escalate now).
    graph.update_task("t1", last_change_digest=hashlib.sha256(b"").hexdigest())

    ready = swarm._return_for_revision(graph.get_task("t1"), [{"type": "build", "error": "boom"}])

    assert ready is False
    assert graph.get_task("t1").status == TaskStatus.MERGED  # adjudicated done
    assert tech_lead.adjudication_calls


def test_no_change_loop_escalates_to_tech_lead(tmp_path, monkeypatch):
    """An engineer that keeps re-flagging done with no diff is handed to the Tech Lead, not spun
    to the 20-revision cap."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 20)
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "2")
    _patch_git(monkeypatch)  # constant empty diff → every round is a no-change round
    tech_lead = StubTechLead(approved=False, adjudication_verdict="done")
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=50)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.MERGED
    assert task.resolved_without_changes is True
    assert tech_lead.adjudication_calls  # escalated
    assert task.revision_count < orch_mod.MAX_TASK_REVISIONS  # did NOT grind to the 20-cap


def test_changing_diff_keeps_revising_without_escalation(tmp_path, monkeypatch):
    """A task that actually changes its code each round keeps its full revision budget and is
    bounded by MAX_TASK_REVISIONS, never escalated as a no-change loop."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 5)
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "3")
    counter = {"n": 0}

    def changing_diff(*a, **k):
        counter["n"] += 1
        return f"diff revision {counter['n']}"  # different every call → always progress

    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", changing_diff)
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: (True, "ok"))
    tech_lead = StubTechLead(approved=False, adjudication_verdict="fail")
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=50)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.FAILED  # bounded by the 20-style MAX cap (here 5)
    assert task.revision_count >= 5  # revised well past the no-change cap of 3
    assert tech_lead.adjudication_calls == []  # never escalated — real progress each round


def test_whole_job_already_complete_when_all_resolved_without_changes(tmp_path, monkeypatch):
    """A job whose only terminal tasks are already-done resolutions reports already_complete."""

    class StubTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
                "already_complete": False,
                "completion_evidence": "",
            }

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            # Mark t1 terminal as an already-done resolution (no real diff landed).
            self.graph.update_task("t1", resolved_without_changes=True)
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert updates[-1]["status"] == "already_complete"
    assert updates[-1]["already_complete"] is True


def test_not_already_complete_when_a_task_is_left_non_terminal(tmp_path, monkeypatch):
    """already_complete requires EVERY task terminal: a resolved-without-changes task alongside a
    still-pending (TO_DO) task is a normal completion, not an 'already complete, recommend closing'
    no-op — the swarm can exit at max_rounds with unfinished work and must not abandon it."""

    class StubTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}, {"id": "t2", "title": "T2"}],
                "stacks": [{"name": "backend", "tools_services": []}],
                "already_complete": False,
                "completion_evidence": "",
            }

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            # t1 resolves as already-done; t2 is left TO_DO (the loop ran out of rounds before
            # finishing it) — a non-terminal task that must block the already_complete verdict.
            self.graph.update_task("t1", resolved_without_changes=True)
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert updates[-1]["status"] == "completed"  # not already_complete
    assert updates[-1].get("already_complete") is not True


def test_return_for_revision_persists_revision_count_on_accept_as_is(tmp_path, monkeypatch):
    """When the gate path accepts a task as-is at the revision cap, the incremented revision_count
    is persisted to the graph (consistent with the FAILED/IN_PROGRESS paths), not just bumped on a
    discarded local."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 2)
    # A changing diff each call → _note_revision_progress never escalates, so we reach the cap check.
    counter = {"n": 0}

    def changing_diff(*a, **k):
        counter["n"] += 1
        return f"diff {counter['n']}"

    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", changing_diff)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=False), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", revision_count=1)  # one below the cap of 2

    ready = swarm._return_for_revision(graph.get_task("t1"), [{"type": "build", "error": "boom"}])

    assert ready is True  # accepted as-is at the cap
    assert graph.get_task("t1").revision_count == 2  # the bump was persisted, not lost


def test_planning_already_complete_short_circuits_swarm(tmp_path, monkeypatch):
    """When the Tech Lead judges the issue already done at planning time, no swarm runs."""

    class StubTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [],
                "stacks": [{"name": "backend", "tools_services": []}],
                "already_complete": True,
                "completion_evidence": "Sub-issues #12 and #13 already merged.",
            }

    class ExplodingSwarm:
        def __init__(self, *a, **k):
            raise AssertionError("swarm must not be built when the work is already complete")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", ExplodingSwarm)

    updates: List[Dict[str, Any]] = []
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert updates[-1]["status"] == "already_complete"
    assert "#12" in updates[-1]["completion_evidence"]


def test_snapshot_restore_preserves_no_change_fields():
    tg = TaskGraphService(job_id="j1")
    tg.add_task("t1", title="T1")
    tg.update_task(
        "t1",
        no_change_revisits=2,
        last_change_digest="abc123",
        resolved_without_changes=True,
    )

    tg2 = TaskGraphService(job_id="j1")
    tg2.restore(tg.snapshot())

    task = tg2.get_task("t1")
    assert task.no_change_revisits == 2
    assert task.last_change_digest == "abc123"
    assert task.resolved_without_changes is True


# ----------------------------------------------------- full review evidence (never truncated)


def test_build_review_evidence_includes_summary_and_diff():
    ev = orch_mod._build_review_evidence("SUMMARY", "DIFFDATA")
    assert "SUMMARY" in ev and "DIFFDATA" in ev and "--- DIFF ---" in ev


def test_build_review_evidence_passes_large_diff_uncut():
    big = "D" * 200000
    ev = orch_mod._build_review_evidence("SUM", big)
    assert ev.startswith("SUM")
    assert big in ev  # full diff passed through, never truncated
    assert "diff truncated" not in ev


def test_build_review_evidence_no_diff():
    assert orch_mod._build_review_evidence("ONLY SUMMARY", "") == "ONLY SUMMARY"


# ----------------------------------------------------- live progress reporting (code review)


def test_quality_gate_code_review_reports_live_progress(tmp_path, monkeypatch):
    """The quality-gate code review must surface the agent's sub-step reports as
    status_text + structured current_activity (rising fraction), then clear the
    activity on completion so a stale sub-bar never lingers."""
    import types

    monkeypatch.setattr(
        f"{QG}.run_build_verification",
        lambda *a, **k: types.SimpleNamespace(success=True, error=""),
    )
    monkeypatch.setattr(f"{QG}.run_linting", lambda *a, **k: None)

    def _fake_review(**kwargs):
        cb = kwargs.get("progress_callback")
        assert cb is not None, "orchestrator must pass a progress callback"
        cb("reviewing", "chunk 1/2: a.py", 0.3)
        cb("reviewing", "chunk 2/2: b.py", 0.7)
        cb("done", "approved=True, issues=0", 1.0)
        return types.SimpleNamespace(approved=True, issues=[])

    monkeypatch.setattr(f"{QG}.run_code_review", _fake_review)

    updates: List[Dict[str, Any]] = []
    swarm, graph = _make_real_swarm(tmp_path)
    swarm._implement_and_verify(swarm.workers[0], lambda **kw: updates.append(kw))

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW

    review_updates = [u for u in updates if "Code review (" in (u.get("status_text") or "")]
    assert review_updates, f"expected code-review status updates, got {updates}"
    assert any("chunk 1/2" in u["status_text"] for u in review_updates)

    activities = [u["current_activity"] for u in updates if u.get("current_activity")]
    assert all(a["agent"] == "code_review" for a in activities)
    fractions = [a["fraction"] for a in activities]
    assert fractions == sorted(fractions), "fractions must be non-decreasing"

    # The final activity-bearing update must be followed by an explicit clear.
    clears = [u for u in updates if "current_activity" in u and u["current_activity"] is None]
    assert clears, "current_activity must be cleared after the review"


def test_tech_lead_review_reports_progress_and_clears_activity(tmp_path, monkeypatch):
    """_review_and_merge bridges Tech Lead reports into the job record and clears
    current_activity after each task's review (success and rejection alike)."""
    _patch_git(monkeypatch)

    class ReportingTechLead(StubTechLead):
        def run_code_review(
            self,
            task_title,
            task_description,
            acceptance_criteria,
            changes_summary,
            user_decisions=None,
            progress_callback=None,
        ):
            if progress_callback is not None:
                progress_callback("reviewing", "attempt 1/3", 0.1)
                progress_callback("done", "review complete", 1.0)
            return super().run_code_review(
                task_title,
                task_description,
                acceptance_criteria,
                changes_summary,
                user_decisions=user_decisions,
            )

    tech_lead = ReportingTechLead(approved=True)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    updates: List[Dict[str, Any]] = []
    swarm._review_and_merge(lambda **kw: updates.append(kw))

    tl_updates = [
        u for u in updates if (u.get("current_activity") or {}).get("agent") == "tech_lead_review"
    ]
    assert tl_updates, f"expected tech-lead activity updates, got {updates}"
    assert any("attempt 1/3" in (u.get("status_text") or "") for u in updates)
    assert any("current_activity" in u and u["current_activity"] is None for u in updates)


def test_orchestrator_does_not_stamp_activity_and_terminal_clears(tmp_path, monkeypatch):
    """last_activity_at is stamped centrally by the job service on every real update,
    so the orchestrator must NOT stamp it client-side (one clock, one writer). The
    terminal update must carry current_activity=None so a transient failure of an
    earlier best-effort clear cannot leave a terminal job serving a stale entry."""
    updates: List[Dict[str, Any]] = []

    class _PlanningTechLead:
        def run_plan_to_task_graph(self, plan_input):
            return {"tasks": [], "stacks": [{"name": "backend", "tools_services": []}]}

    class _NoopSwarm:
        aborted = False

        def __init__(self, **kw):
            self.graph = kw["graph"]

        def run(self, **kw):
            kw["update_fn"](status_text="working")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda *a, **k: _PlanningTechLead())
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", _NoopSwarm)

    run_coding_team_orchestrator(
        job_id="j-activity",
        plan_input=CodingTeamPlanInput(
            plan="p", specification="s", architecture="a", repo_path=str(tmp_path)
        ),
        repo_path=str(tmp_path),
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {},
        get_llm=lambda key: None,
    )

    assert updates, "orchestrator must have written job updates"
    for kw in updates:
        assert "last_activity_at" not in kw, f"client-side stamp leaked into {kw}"

    final = updates[-1]
    assert final.get("status") == "completed"
    assert "current_activity" in final and final["current_activity"] is None


# ----------------------------------------------------- tech lead review progress reporting


def test_tech_lead_review_progress_reports_attempts_and_retry_waits(monkeypatch):
    """A flaky review reports attempt 1/N, the backoff wait, attempt 2/N, then a
    terminal done at 1.0 — silent retries are the prime 'looks hung' source."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "2")  # → 3 attempts
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def flaky(agent, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return {"approved": True, "reason": "ok", "requested_changes": []}

    monkeypatch.setattr(tl_mod, "_agent_call_json", flaky)
    tl = tl_mod.TechLeadAgent(model=object())

    reports: List[Any] = []
    out = tl.run_code_review(
        "t", "d", [], "evidence", progress_callback=lambda s, d, f: reports.append((s, d, f))
    )

    assert out["approved"] is True
    steps_details = [(s, d) for s, d, _f in reports]
    assert steps_details[0] == ("reviewing", "attempt 1/3")
    assert steps_details[1][0] == "waiting_retry"
    assert "attempt 1/3 failed" in steps_details[1][1]
    assert steps_details[2] == ("reviewing", "attempt 2/3")
    assert reports[-1][0] == "done"
    assert reports[-1][2] == 1.0


def test_tech_lead_review_progress_terminal_done_on_exhausted_retries(monkeypatch):
    """Exhausted retries still emit a terminal done at 1.0 (no perpetual mid-bar)."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "1")  # → 2 attempts
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt: (_ for _ in ()).throw(RuntimeError("down")),
    )
    tl = tl_mod.TechLeadAgent(model=object())

    reports: List[Any] = []
    out = tl.run_code_review(
        "t", "d", [], "x", progress_callback=lambda s, d, f: reports.append((s, d, f))
    )

    assert out["error"] is True
    assert reports[-1][0] == "done"
    assert reports[-1][2] == 1.0
    assert "failed after 2 attempt(s)" in reports[-1][1]
    assert "after 2 attempt(s)" in out["reason"]


def test_tech_lead_review_fail_fast_reports_actual_attempt_count(monkeypatch):
    """A fail-fast error (rate limit) skips retries entirely; the diagnostic must say
    1 attempt — claiming the full budget misleads the operator about what ran."""
    from coding_team.tech_lead_agent import agent as tl_mod
    from llm_service.interface import LLMRateLimitError

    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "2")  # → budget of 3 attempts
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt: (_ for _ in ()).throw(LLMRateLimitError("429")),
    )
    tl = tl_mod.TechLeadAgent(model=object())

    reports: List[Any] = []
    out = tl.run_code_review(
        "t", "d", [], "x", progress_callback=lambda s, d, f: reports.append((s, d, f))
    )

    assert out["error"] is True
    assert "after 1 attempt(s)" in out["reason"]
    assert "failed after 1 attempt(s)" in reports[-1][1]


def test_tech_lead_review_raising_callback_never_burns_attempts(monkeypatch):
    """A raising progress_callback is an observability bug, not an LLM failure: it must
    be swallowed, never counted as a failed attempt, and the review must succeed."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    calls = {"n": 0}

    def ok(agent, prompt):
        calls["n"] += 1
        return {"approved": True, "reason": "ok", "requested_changes": []}

    monkeypatch.setattr(tl_mod, "_agent_call_json", ok)
    tl = tl_mod.TechLeadAgent(model=object())

    def _boom(s, d, f):
        raise RuntimeError("store down")

    out = tl.run_code_review("t", "d", [], "evidence", progress_callback=_boom)
    assert out["approved"] is True and out["error"] is False
    assert calls["n"] == 1, "exactly one LLM attempt; callback errors must not retry"


def test_tech_lead_review_no_callback_unchanged(monkeypatch):
    """progress_callback omitted → result identical to the pre-callback behavior."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt: {"approved": True, "reason": "ok", "requested_changes": []},
    )
    tl = tl_mod.TechLeadAgent(model=object())
    out = tl.run_code_review("t", "d", [], "evidence")
    assert out == {"approved": True, "error": False, "reason": "ok", "requested_changes": []}


# ----------------------------------------------------- job-level progress (coding band)


def test_coding_progress_maps_terminal_share_onto_band():
    """Empty graph → base; progress is monotone in terminal tasks, bounded by the band,
    and respects a caller-supplied (base, span) slice (terminal 100 is written separately)."""

    def tasks(merged: int, failed: int, open_: int):
        return (
            [{"status": "merged"}] * merged
            + [{"status": "failed"}] * failed
            + [{"status": "to_do"}] * open_
        )

    for base, span in [(0, 95), (30, 65)]:
        assert orch_mod._coding_progress([], base, span) == base
        assert orch_mod._coding_progress(tasks(0, 0, 4), base, span) == base
        half = orch_mod._coding_progress(tasks(1, 1, 2), base, span)
        full = orch_mod._coding_progress(tasks(3, 1, 0), base, span)
        assert base <= half <= full == base + span <= 100

    # An impossible band is a caller bug, not something to render.
    with pytest.raises(AssertionError):
        orch_mod._coding_progress([], 50, 60)


def test_orchestrator_writes_job_progress_through_coding_phase(tmp_path, monkeypatch):
    """The job-level progress bar must advance during the coding phase: base at coding
    start, per-snapshot updates from the task graph, and 100 on terminal completion."""
    updates: List[Dict[str, Any]] = []

    class _PlanningTechLead:
        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}, {"id": "t2", "title": "T2"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    class _MergingSwarm:
        aborted = False

        def __init__(self, **kw):
            self.graph = kw["graph"]

        def run(self, **kw):
            # Simulate one task reaching a terminal state mid-run; persist_fn must
            # carry the recomputed progress.
            self.graph.update_task("t1", status=TaskStatus.MERGED)
            kw["persist_fn"]()

    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda *a, **k: _PlanningTechLead())
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", _MergingSwarm)

    run_coding_team_orchestrator(
        job_id="j-progress",
        plan_input=CodingTeamPlanInput(
            plan="p", specification="s", architecture="a", repo_path=str(tmp_path)
        ),
        repo_path=str(tmp_path),
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {},
        get_llm=lambda key: None,
    )

    progresses = [kw["progress"] for kw in updates if "progress" in kw]
    assert progresses, "orchestrator must write job-level progress"
    assert progresses[0] == orch_mod._DEFAULT_PROGRESS_BASE
    # 1 of 2 tasks terminal mid-run
    expected_mid = orch_mod._DEFAULT_PROGRESS_BASE + int(orch_mod._DEFAULT_PROGRESS_SPAN / 2)
    assert expected_mid in progresses
    assert progresses[-1] == 100
    assert progresses == sorted(progresses), "progress must be monotone non-decreasing"


def test_orchestrator_resume_never_regresses_progress(tmp_path, monkeypatch):
    """Resuming from a snapshot with terminal tasks must not regress the bar: the
    restored persist publishes the band value for the already-merged share, and no
    later write may go below it (the old unconditional base write went 52 → 10)."""
    updates: List[Dict[str, Any]] = []

    snapshot = [
        {"id": "t1", "title": "T1", "status": "merged", "dependencies": []},
        {"id": "t2", "title": "T2", "status": "to_do", "dependencies": []},
    ]

    class _NoopSwarm:
        aborted = False

        def __init__(self, **kw):
            self.graph = kw["graph"]

        def run(self, **kw):
            kw["persist_fn"]()

    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda *a, **k: object())
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", _NoopSwarm)

    run_coding_team_orchestrator(
        job_id="j-resume-progress",
        plan_input=CodingTeamPlanInput(
            plan="p", specification="s", architecture="a", repo_path=str(tmp_path)
        ),
        repo_path=str(tmp_path),
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {"task_graph_snapshot": snapshot, "agent_task_map": {}},
        get_llm=lambda key: None,
    )

    progresses = [kw["progress"] for kw in updates if "progress" in kw]
    restored = orch_mod._DEFAULT_PROGRESS_BASE + int(orch_mod._DEFAULT_PROGRESS_SPAN / 2)
    assert progresses[0] == restored, "restored persist must reflect merged tasks"
    assert progresses == sorted(progresses), "no write may regress the bar on resume"
    assert progresses[-1] == 100


# ------------------------------------ user decisions surfaced to the review gates


def _capture_review_prompt(monkeypatch):
    """Patch the Tech Lead's LLM call to record the rendered review prompt and approve."""
    from coding_team.tech_lead_agent import agent as tl_mod

    captured: Dict[str, str] = {}

    def _record(agent, prompt):
        captured["prompt"] = prompt
        return {"approved": True, "reason": "ok", "requested_changes": []}

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(tl_mod, "_agent_call_json", _record)
    return tl_mod.TechLeadAgent(model=object()), captured


def test_review_prompt_includes_user_decisions(monkeypatch):
    """A settled decision is rendered into the review prompt so the reviewer does not re-raise it."""
    tl, captured = _capture_review_prompt(monkeypatch)

    out = tl.run_code_review(
        "t", "d", [], "evidence", user_decisions=["Which auth? → OAuth2 (Google)"]
    )

    assert out["approved"] is True
    assert "User decisions already made" in captured["prompt"]
    assert "Which auth? → OAuth2 (Google)" in captured["prompt"]


def test_review_prompt_omits_decisions_block_when_none(monkeypatch):
    """No decisions → prompt is unchanged from the pre-feature behavior (no stray header)."""
    tl, captured = _capture_review_prompt(monkeypatch)

    tl.run_code_review("t", "d", [], "evidence")
    assert "User decisions already made" not in captured["prompt"]

    # Empty / whitespace-only entries are dropped, not rendered as an empty block.
    captured.clear()
    tl.run_code_review("t", "d", [], "evidence", user_decisions=["", "   "])
    assert "User decisions already made" not in captured["prompt"]


def test_user_decisions_for_combines_plan_and_task_levels(tmp_path):
    """_user_decisions_for merges plan-level resolved questions with task-level escalations
    and de-duplicates by normalized question text (case-insensitively)."""
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
        resolved_questions=[{"question_text": "Which DB?", "answer": "Postgres"}],
    )
    task = Task(
        id="t1",
        title="T1",
        revision_feedback=[
            {
                "source": "user_decision",
                "reason": "ignored-by-render",
                "decisions": [{"question_text": "Use TLS?", "answer": "Yes, TLS 1.3"}],
            },
            # A second escalation repeating the plan-level question (different case, same answer)
            # must collapse onto one line rather than double-render.
            {
                "source": "user_decision",
                "decisions": [{"question_text": "which db?", "answer": "Postgres"}],
            },
            {"source": "tech_lead", "reason": "unrelated", "requested_changes": []},
        ],
    )

    lines = swarm._user_decisions_for(task)

    assert "Use TLS? → Yes, TLS 1.3" in lines
    db_lines = [ln for ln in lines if ln.lower() == "which db? → postgres"]
    assert len(db_lines) == 1, f"repeated DB question must collapse, got {lines}"
    assert len(lines) == 2, f"got {lines}"


def test_user_decisions_for_latest_answer_wins_for_same_question(tmp_path):
    """Dedup is by question text with last-answer-wins: a task-level escalation overrides the
    plan-level answer for the same question, so the reviewer is never shown two conflicting answers."""
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
        resolved_questions=[{"question_text": "Which DB?", "answer": "Postgres"}],
    )
    task = Task(
        id="t1",
        title="T1",
        revision_feedback=[
            {
                "source": "user_decision",
                "decisions": [{"question_text": "Which DB?", "answer": "MySQL"}],
            }
        ],
    )

    lines = swarm._user_decisions_for(task)

    # The later (task-level) answer supersedes the earlier (plan-level) one; only one line survives.
    assert lines == ["Which DB? → MySQL"], f"latest answer must win, got {lines}"


def test_user_decisions_for_falls_back_to_reason_for_legacy_entry(tmp_path):
    """A user_decision entry predating the structured 'decisions' field (resumed across an upgrade)
    still surfaces its decision via the rendered 'reason' text, rather than being dropped."""
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
    )
    task = Task(
        id="t1",
        title="T1",
        # Legacy shape: only 'reason', no structured 'decisions'.
        revision_feedback=[{"source": "user_decision", "reason": "Use TLS? → Yes"}],
    )

    lines = swarm._user_decisions_for(task)

    assert lines == ["Use TLS? → Yes"]


def test_user_decisions_for_empty_decisions_does_not_fall_back_to_reason(tmp_path):
    """A NEW entry carrying an empty structured 'decisions' list contributes nothing — it must NOT
    spill its generic 'reason' sentence into the decisions list (presence-gated, not truthiness)."""
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
    )
    task = Task(
        id="t1",
        title="T1",
        revision_feedback=[
            {
                "source": "user_decision",
                "reason": "The user answered the open question(s) you raised.",
                "decisions": [],
            }
        ],
    )

    assert swarm._user_decisions_for(task) == []


def test_user_decisions_for_handles_answer_only_lines(tmp_path):
    """Answer-only decision records (no question_text) render as the bare answer and dedupe against
    identical answer-only lines (case-insensitively), without assuming a '→' separator."""
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
        resolved_questions=[{"answer": "Use TLS"}],  # plan-level, answer-only
    )
    task = Task(
        id="t1",
        title="T1",
        revision_feedback=[
            {
                "source": "user_decision",
                "decisions": [
                    {"answer": "Use TLS"},  # identical answer → deduped against the plan-level one
                    {"answer": "Use SSL"},  # distinct answer → kept
                ],
            }
        ],
    )

    lines = swarm._user_decisions_for(task)

    assert "Use TLS" in lines  # rendered as the bare answer (no "→")
    assert "Use SSL" in lines
    assert len(lines) == 2, f"identical answer-only lines must collapse, got {lines}"


def test_user_decisions_for_legacy_multiline_reason_extracts_bullets(tmp_path):
    """A legacy entry whose 'reason' is the full multi-line block contributes clean per-decision
    lines (the preamble is dropped, the '- q → a' bullets are extracted), not one messy line."""
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
    )
    reason = (
        "The user answered the open question(s) you raised. Implement these decisions exactly; "
        "do not ask again:\n- Which DB? → Postgres\n- Use TLS? → Yes"
    )
    task = Task(
        id="t1",
        title="T1",
        revision_feedback=[{"source": "user_decision", "reason": reason}],
    )

    lines = swarm._user_decisions_for(task)

    assert lines == ["Which DB? → Postgres", "Use TLS? → Yes"]


def test_review_and_merge_passes_user_decisions(tmp_path, monkeypatch):
    """_review_and_merge feeds the task's settled decisions to the Tech Lead reviewer."""
    _patch_git(monkeypatch)
    tech_lead = StubTechLead(approved=True)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    swarm.resolved_questions = [{"question_text": "Which DB?", "answer": "Postgres"}]
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task(
        "t1",
        revision_feedback=[
            {
                "source": "user_decision",
                "decisions": [{"question_text": "Use TLS?", "answer": "Yes"}],
            }
        ],
    )
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    assert tech_lead.decision_calls[-1] == ["Which DB? → Postgres", "Use TLS? → Yes"]


def test_quality_gate_review_receives_user_decisions(tmp_path, monkeypatch):
    """The per-task quality-gate code review is also told the user's settled decisions."""
    import types

    swarm, graph = _make_real_swarm(tmp_path)
    swarm.resolved_questions = [{"question_text": "Which DB?", "answer": "Postgres"}]
    graph.update_task(
        "t1",
        revision_feedback=[
            {
                "source": "user_decision",
                "decisions": [{"question_text": "Use TLS?", "answer": "Yes"}],
            }
        ],
    )

    captured: Dict[str, Any] = {}

    def _fake_review(**kw):
        captured["user_decisions"] = kw.get("user_decisions")
        return types.SimpleNamespace(approved=True, issues=[])

    monkeypatch.setattr(
        f"{QG}.run_build_verification",
        lambda *a, **k: types.SimpleNamespace(success=True, error=""),
    )
    monkeypatch.setattr(f"{QG}.run_linting", lambda *a, **k: None)
    monkeypatch.setattr(f"{QG}.run_code_review", _fake_review)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert captured["user_decisions"] == ["Which DB? → Postgres", "Use TLS? → Yes"]
