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

GIT_UTILS = "shared_git.git_utils"


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


class _FakeWorktreeManager:
    """Test double for coding_team.worktree_manager.WorktreeManager.

    These orchestrator tests stub the implementation workers entirely
    (StubWorker never touches git), so a real WorktreeManager would pay for
    (and, for the ~20 call sites with no _patch_git, actually attempt) real
    `git worktree add` calls against a plain tmp_path with no `.git`. This
    double gives each agent_id a distinct child directory under the swarm's
    own tmp_path instead — no git, no filesystem writes outside tmp_path's own
    pytest-managed cleanup. Real worktree mechanics are covered by
    test_worktree_manager.py against WorktreeManager itself.
    """

    def __init__(self, repo_path: Path, agent_ids):
        self._paths = {aid: Path(repo_path) / f"_wt_{aid}" for aid in agent_ids}
        self.prepare_calls = 0
        self.cleanup_calls = 0

    def prepare(self) -> None:
        self.prepare_calls += 1
        for path in self._paths.values():
            path.mkdir(parents=True, exist_ok=True)

    def path_for(self, agent_id: str) -> Path:
        return self._paths[agent_id]

    def cleanup(self) -> None:
        self.cleanup_calls += 1


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
    # Real worktree creation is exercised in test_worktree_manager.py; give these
    # stub-worker tests a git-free stand-in instead (see _FakeWorktreeManager).
    swarm._worktrees = _FakeWorktreeManager(swarm.path, [w.agent_id for w in workers])
    swarm._worktrees.prepare()
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

    def flaky(agent, prompt, required_keys=None):
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

    def boom(agent, prompt, required_keys=None):
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
    monkeypatch.setattr(
        tl_mod, "_agent_call_json", lambda agent, prompt, required_keys=None: {"reason": "hmm"}
    )
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
        lambda agent, prompt, required_keys=None: {"approved": False, "reason": "needs tests"},
    )
    tl = tl_mod.TechLeadAgent(model=object())

    out = tl.run_code_review("t", "d", [], "evidence")

    assert out["error"] is False
    assert out["approved"] is False
    assert out["reason"] == "needs tests"


def test_review_non_bool_approved_is_infra_failure_not_rejection(monkeypatch):
    """A fabricated non-boolean ``approved`` (e.g. ``""`` that tolerant repair completes
    from a truncated ``{"approved": ``) must NOT slip through as a substantive rejection.
    The guard raises so it surfaces as an infra failure (error=True), never a silent
    approved=False that would burn a revision round on a verdict the model never gave."""
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setenv("CODING_TEAM_REVIEW_RETRIES", "0")  # single attempt, no backoff waits
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {"approved": "", "reason": ""},
    )
    tl = tl_mod.TechLeadAgent(model=object())

    out = tl.run_code_review("t", "d", [], "evidence")

    assert out["error"] is True
    assert out["approved"] is False  # fail-closed default, not the fabricated verdict


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
    from shared_git.git_utils import (
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
    from shared_git.git_utils import branch_diff

    assert branch_diff(tmp_path / "does-not-exist", "development", "feature/x") == ""


def test_branch_diff_bad_branch_returns_empty(tmp_path):
    """A failing git diff (e.g. unknown branch) yields "" rather than raising."""
    from shared_git.git_utils import branch_diff, initialize_new_repo

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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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


def _gate_provider(*, build_ok=True, review_ok=True, build_raises=False):
    """A fake CodeEngineProvider exposing just the quality-gate methods the swarm calls."""
    import types

    class _FakeGateProvider:
        def run_build_verification(self, *a, **k):
            if build_raises:
                raise RuntimeError("tool crashed")
            return types.SimpleNamespace(success=build_ok, error="" if build_ok else "boom build")

        def run_linting(self, *a, **k):
            return None

        def run_code_review(self, **k):
            return types.SimpleNamespace(
                approved=review_ok,
                issues=[] if review_ok else [{"type": "review", "error": "x"}],
            )

    return _FakeGateProvider()


def _make_real_swarm(tmp_path, provider):
    """A swarm WITHOUT the _run_quality_gates bypass, with one task already assigned to a1.

    ``provider`` supplies the build/lint/review engines (see ``_gate_provider``).
    """
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[StubWorker("a1")],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["a1"],
        llm_getter=lambda k: None,
        engine_provider=provider,
    )
    swarm._worktrees = _FakeWorktreeManager(swarm.path, ["a1"])
    swarm._worktrees.prepare()
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    return swarm, graph


def test_quality_gates_pass_sets_in_review(tmp_path):
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=True, review_ok=True))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


def test_quality_gates_skip_with_warning_when_no_engine_provider(tmp_path):
    """No engine_provider configured (an embedder wired the swarm directly, without injecting
    build/lint/review engines) → gates are skipped, not silently: a SKIPPED status is reported
    and the task still proceeds straight to review."""
    swarm, graph = _make_real_swarm(tmp_path, None)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


def test_quality_gates_suppress_code_review_bridge_when_live_progress_false(tmp_path):
    """live_progress=False (the concurrent fan-out path) uses a no-op code-review progress
    bridge — it must never construct a live ActivityBridge, which would race the one
    sub-progress slot against other concurrently-running workers' bridges."""
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=True, review_ok=True))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None, live_progress=False)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


def test_quality_gate_build_failure_returns_for_revision(tmp_path):
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=False))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO  # returned for revision
    assert task.assigned_agent_id is None  # and unassigned


def test_quality_gate_review_rejection_returns_for_revision(tmp_path):
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(review_ok=False))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.TO_DO


def test_quality_gate_tool_exception_proceeds_to_review(tmp_path):
    """An unexpected quality-gate tool error is logged and the task still proceeds (best-effort)."""
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_raises=True))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW  # proceeded despite the tool error


def test_quality_gate_tool_exception_logs_full_traceback(tmp_path, caplog):
    """An unexpected quality-gate tool error must be logged WITH a full traceback
    (logger.exception → ERROR + exc_info), not a one-line WARNING — a silent
    summary is exactly what made the review-phase crash undebuggable."""
    import logging as _logging

    # build() raises RuntimeError("tool crashed")
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_raises=True))

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

    # review rejects
    monkeypatch.setattr(orch_mod, "ActivityBridge", _boom)
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(review_ok=False))

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


def test_pinned_task_reassigned_only_to_originating_agent(tmp_path):
    """A task whose feature branch is pinned to an agent (feature_branch_agent_id) is
    reassigned ONLY to that agent — never to a different free agent — even for a
    target_team-less task that would otherwise match anyone. This is what prevents a
    revision-rejected task's branch (checked out only in the pinned agent's worktree) from
    being handed to a different worker, which git would refuse."""

    class MismatchingTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            # Proposes the WRONG (unpinned) agent — the pin must override this.
            return {"assignments": [{"agent_id": "frontend_v2", "task_id": "t1"}]}

    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, MismatchingTL(approved=True), workers)
    # No target_team: _target_matches_agent would otherwise permit ANY agent.
    graph.add_task("t1", title="T1")
    graph.update_task("t1", feature_branch="feature/t1", feature_branch_agent_id="backend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    task = graph.get_task("t1")
    assert task.assigned_agent_id == "backend_v2"  # pinned agent, not the LLM's proposal
    assert graph.get_task_for_agent("frontend_v2") is None


def test_pinned_task_stays_unassigned_when_pinned_agent_not_free(tmp_path):
    """A pinned task never falls back to a different free agent — it waits for its own."""
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), workers)
    graph.add_task("t1", title="T1")
    graph.update_task("t1", feature_branch="feature/t1", feature_branch_agent_id="backend_v2")

    # Only frontend_v2 is free this round; backend_v2 (the pin) is not.
    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2"])

    assert graph.get_task("t1").assigned_agent_id is None
    assert graph.get_task_for_agent("frontend_v2") is None


def test_pinned_task_falls_back_to_unpinned_when_pinned_agent_leaves_roster(tmp_path):
    """A pin to an agent no longer in the roster (e.g. after a roster change across a retry)
    is unenforceable and treated as unpinned, so the task can still be assigned."""
    workers = [StubWorker("frontend_v2")]  # backend_v2 no longer in the roster
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), workers)
    graph.add_task("t1", title="T1")
    graph.update_task("t1", feature_branch="feature/t1", feature_branch_agent_id="backend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2"])

    assert graph.get_task("t1").assigned_agent_id == "frontend_v2"


def test_quality_gate_rejection_preserves_pin_for_reassignment(tmp_path):
    """A task rejected by quality gates keeps its feature_branch_agent_id pin — only
    assigned_agent_id is cleared by unassign_task — so the next round's assignment sends it
    back to the SAME agent even when a different one is free and the Tech Lead proposes it.
    Proves the pin survives the exact demotion path (_return_for_revision) that originally
    motivated it."""
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), workers)
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "backend_v2")

    # Round 1: implement (records feature_branch + pin; _run_quality_gates is stubbed to pass
    # in _make_swarm, so the task reaches review here).
    swarm._implement_and_verify(swarm.workers[1], lambda **kw: None)  # workers[1] == backend_v2
    task = graph.get_task("t1")
    assert task.status == TaskStatus.IN_REVIEW
    assert task.feature_branch_agent_id == "backend_v2"

    # A quality-gate rejection demotes it back to TO_DO/unassigned.
    swarm._return_for_revision(task, [{"type": "build", "error": "boom"}])
    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO
    assert task.assigned_agent_id is None  # unassigned
    assert task.feature_branch_agent_id == "backend_v2"  # pin survives

    # Round 2: both workers free; a mismatching Tech Lead tries to hand it to frontend_v2.
    class MismatchingTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": [{"agent_id": "frontend_v2", "task_id": "t1"}]}

    swarm.tech_lead = MismatchingTL(approved=True)
    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    assert graph.get_task("t1").assigned_agent_id == "backend_v2"  # pinned, not reassigned


def test_pinned_agent_reserved_before_unrelated_tech_lead_assignment(tmp_path):
    """A pinned task's agent is reserved BEFORE any Tech-Lead proposal is processed, so an
    unrelated task's assignment in the same response can never claim it first and starve the
    pinned task — even when the Tech Lead's own (wrong) proposal for the pinned task is listed
    before a proposal that would otherwise legitimately claim the pinned agent for something
    else."""

    class AdversarialOrderingTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {
                "assignments": [
                    # Wrong agent for the pinned task, listed first...
                    {"agent_id": "frontend_v2", "task_id": "pinned_task"},
                    # ...followed by a claim on the pinned agent for an unrelated task, which
                    # must NOT be allowed to steal it out from under the pinned task.
                    {"agent_id": "backend_v2", "task_id": "other_task"},
                ]
            }

    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, AdversarialOrderingTL(approved=True), workers)
    graph.add_task("pinned_task", title="Pinned")
    graph.update_task(
        "pinned_task", feature_branch="feature/pinned", feature_branch_agent_id="backend_v2"
    )
    graph.add_task("other_task", title="Other")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    assert graph.get_task("pinned_task").assigned_agent_id == "backend_v2"
    # other_task lost the race for backend_v2 and stays unassigned this round rather than
    # starving the pinned task — it will be picked up once an agent frees up.
    assert graph.get_task("other_task").assigned_agent_id is None


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


def test_assignment_normalizes_frontend_owned_target_aliases(tmp_path):
    """Frontend-owned target aliases such as ui/ux route to the frontend v2 worker."""

    class AssignUiTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": [{"agent_id": "frontend_v2", "task_id": "screen"}]}

    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, AssignUiTL(approved=True), workers)
    graph.add_task("screen", title="Build settings screen", target_team="ui")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    task = graph.get_task("screen")
    assert task.assigned_agent_id == "frontend_v2"
    assert graph.get_task_for_agent("backend_v2") is None


def test_target_match_normalizes_frontend_owned_aliases() -> None:
    """UI/UX target aliases canonicalize to the frontend v2 team for matching."""
    assert orch_mod._team_key("ui") == "frontend_v2"
    assert orch_mod._team_key("UX") == "frontend_v2"
    assert orch_mod._team_key("Web App") == "frontend_v2"
    assert orch_mod._target_matches_agent("ui", "frontend-v2-worker-2") is True
    assert orch_mod._target_matches_agent("ux", "backend_v2_worker_1") is False
    # Exact-token match only: unrelated words containing an alias substring are unaffected.
    assert orch_mod._team_key("build") == "build"
    assert orch_mod._team_key("guidelines") == "guidelines"


def test_team_key_routes_framework_and_language_labels() -> None:
    """Concrete tech labels route to the owning v2 team instead of failing to match."""
    for label in ("React", "Angular", "AngularJS", "scss", "Next.js", "React.js", "Vue.js"):
        assert orch_mod._team_key(label) == "frontend_v2"
    for label in (
        "Python",
        "Java",
        "FastAPI",
        "Spring Boot",
        "Postgres",
        "Node.js",
        "Express.js",
        ".NET",
        ".NET Core",
        "ASP.NET",
    ):
        assert orch_mod._team_key(label) == "backend_v2"
    # Ambiguous languages (used by frontend frameworks AND Node backends) must NOT be
    # forced onto a team — routing them would mis-send backend work to the frontend worker.
    for label in ("TypeScript", "JavaScript"):
        assert orch_mod._team_key(label) not in ("frontend_v2", "backend_v2")
    # A capable worker now matches these labels rather than the task being dropped.
    assert orch_mod._target_matches_agent("react", "frontend_v2") is True
    assert orch_mod._target_matches_agent("python", "backend_v2") is True
    # Generic, non-tech words still pass through unmapped.
    assert orch_mod._team_key("build") == "build"


def test_team_key_accepts_compact_v2_labels() -> None:
    """Separator-less v2 labels still route, without matching unrelated substrings."""
    assert orch_mod._team_key("frontendv2") == "frontend_v2"
    assert orch_mod._team_key("BackendV2") == "backend_v2"
    # Exact-match only: a word merely containing the alias as a substring is unaffected.
    assert orch_mod._team_key("myfrontend") == "myfrontend"


def test_assign_tasks_survives_assignment_error(tmp_path):
    """A transient assign_task_to_agent error is logged and skipped, not propagated."""

    class OneAssignTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": [{"agent_id": "backend_v2", "task_id": "t1"}]}

    workers = [StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, OneAssignTL(approved=True), workers)
    graph.add_task("t1", title="Build API", target_team="backend_v2")

    def _boom(task_id, agent_id):
        raise RuntimeError("transient store error")

    swarm.graph.assign_task_to_agent = _boom  # type: ignore[method-assign]

    # Must not raise; the task simply stays unassigned for this round.
    swarm._assign_tasks(graph.get_tasks(), ["backend_v2"])
    assert graph.get_task("t1").assigned_agent_id is None
    assert graph.get_task("t1").status == TaskStatus.TO_DO


def test_assign_tasks_continues_to_next_agent_after_error(tmp_path):
    """When the first matching worker's assignment raises, a second free worker still gets it."""

    class NoopTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": []}

    workers = [StubWorker("backend_v2"), StubWorker("backend_v2_alt")]
    swarm, graph = _make_swarm(tmp_path, NoopTL(approved=True), workers)
    graph.add_task("t1", title="Build API", target_team="backend_v2")

    real_assign = graph.assign_task_to_agent

    def _flaky(task_id, agent_id):
        if agent_id == "backend_v2":
            raise RuntimeError("transient store error")
        return real_assign(task_id, agent_id)

    swarm.graph.assign_task_to_agent = _flaky  # type: ignore[method-assign]

    # The guardrail loop skips the failing worker and places the task on the second one.
    swarm._assign_tasks(graph.get_tasks(), ["backend_v2", "backend_v2_alt"])
    assert graph.get_task("t1").assigned_agent_id == "backend_v2_alt"


def test_assignment_fails_task_with_unrecognized_target_team(tmp_path, caplog):
    """A target_team that no worker can satisfy fails with blocked dependents."""

    class NoopTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": []}

    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, NoopTL(approved=True), workers)
    graph.add_task("unknown", title="Unknown target", target_team="unknown_team")
    graph.add_task("blocked", title="Blocked follow-up", dependencies=["unknown"])

    with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
        swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    task = graph.get_task("unknown")
    assert task.status == TaskStatus.FAILED
    assert task.assigned_agent_id is None
    assert "No implementation worker is available" in task.changes_summary
    assert task.revision_feedback[-1]["source"] == "system"
    assert graph.get_task("blocked").status == TaskStatus.FAILED
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


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("front-end", "frontend_v2"),
        ("front end", "frontend_v2"),
        ("back-end", "backend_v2"),
        ("back end", "backend_v2"),
    ],
)
def test_team_key_normalizes_separated_frontend_backend_labels(label: str, expected: str) -> None:
    """Common separated frontend/backend labels route to canonical v2 teams."""
    assert orch_mod._team_key(label) == expected


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
        orch_mod._v2_team_kind_for_stack(StackSpec(name="documentation", tools_services=["guides"]))
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
    assert (
        orch_mod._v2_team_kind_for_stack(StackSpec(name=stack_name, tools_services=[])) == "backend"
    )


@pytest.mark.parametrize("stack_name", ["default", "Senior Software Engineer"])
def test_v2_team_kind_accepts_legacy_default_stack_names(stack_name: str) -> None:
    """Legacy generic stack names now route to backend v2 after removing the Senior SWE worker."""
    assert (
        orch_mod._v2_team_kind_for_stack(StackSpec(name=stack_name, tools_services=[])) == "backend"
    )


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

    def _build_worker(agent_id, spec, llm_getter, engine_provider):
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


def test_backend_v2_worker_uses_injected_llm_getter():
    """Backend v2 worker construction must honor the coding-team LLM injection path.

    The implementation team lead comes from the injected CodeEngineProvider, not a
    direct software_engineering_team import.
    """
    captured_keys: List[str] = []

    class _FakeLead:
        def __init__(self, llm):
            self.llm = llm

    class _FakeProvider:
        def build_implementation_team_lead(self, team_kind, llm):
            return _FakeLead(llm)

    def _llm_getter(key: str) -> str:
        captured_keys.append(key)
        return f"{key}-client"

    worker = orch_mod._build_implementation_worker(
        "backend_v2",
        StackSpec(name="backend_v2", tools_services=["Python"]),
        _llm_getter,
        _FakeProvider(),
    )

    assert captured_keys == ["backend"]
    assert worker.team_lead.llm == "backend-client"


def test_v2_worker_clones_injected_strands_model_to_text_mode():
    """Cloneable JSON-mode Strands models are passed to v2 teams in text mode."""

    class _CloneableModel:
        def __init__(self, response_format: str) -> None:
            self.response_format = response_format
            self.clones: List[str] = []

        def get_config(self) -> Dict[str, str]:
            return {"response_format": self.response_format}

        def clone(self, **overrides):
            response_format = overrides.get("response_format", self.response_format)
            self.clones.append(response_format)
            return _CloneableModel(response_format)

    class _FakeLead:
        def __init__(self, llm):
            self.llm = llm

    class _FakeProvider:
        def build_implementation_team_lead(self, team_kind, llm):
            return _FakeLead(llm)

    model = _CloneableModel("json")

    worker = orch_mod._build_implementation_worker(
        "backend_v2",
        StackSpec(name="backend_v2", tools_services=["Python"]),
        lambda key: model,
        _FakeProvider(),
    )

    assert model.response_format == "json"
    assert model.clones == ["text"]
    assert worker.team_lead.llm.response_format == "text"


def test_v2_text_mode_llm_resolves_underlying_client_on_clone_failure(monkeypatch):
    """A clone() failure re-resolves text mode from the wrapped client, not the JSON-mode model.

    resolve_strands_model returns a pre-built Strands Model as-is, so passing the original
    model back would leak JSON mode. Re-resolving from the model's ``_client`` guarantees a
    fresh text-mode wrapper.
    """
    import llm_service.strands_model as strands_model_mod

    received: Dict[str, Any] = {}
    sentinel = object()

    def _fake_resolve(llm):
        received["arg"] = llm
        return sentinel

    monkeypatch.setattr(strands_model_mod, "resolve_text_mode_strands_model", _fake_resolve)

    client = object()

    class _BrokenJsonModel:
        _client = client

        def get_config(self):
            return {"response_format": "json"}

        def clone(self, **_overrides):
            raise RuntimeError("clone boom")

    model = _BrokenJsonModel()
    result = orch_mod._v2_text_mode_llm(model)

    assert result is sentinel
    # Re-resolved from the wrapped client (guaranteed text mode), not the JSON-mode model.
    assert received["arg"] is client
    assert received["arg"] is not model


def test_v2_text_mode_llm_clone_failure_without_client_uses_default(monkeypatch):
    """A clone() failure on a handle with no ``_client`` falls back to a fresh text model."""
    import llm_service.strands_model as strands_model_mod

    received: Dict[str, Any] = {}
    sentinel = object()

    def _fake_resolve(llm):
        received["arg"] = llm
        return sentinel

    monkeypatch.setattr(strands_model_mod, "resolve_text_mode_strands_model", _fake_resolve)

    class _BrokenCloneModel:
        def get_config(self):
            return {"response_format": "json"}

        def clone(self, **_overrides):
            raise RuntimeError("clone boom")

    broken = _BrokenCloneModel()
    result = orch_mod._v2_text_mode_llm(broken)

    # No wrapped client → resolver builds a fresh default text model (arg is None).
    assert result is sentinel
    assert received["arg"] is None
    assert result is not broken


def test_frontend_v2_worker_uses_injected_llm_getter():
    """Frontend v2 worker construction must honor the coding-team LLM injection path."""
    captured_keys: List[str] = []

    class _FakeLead:
        def __init__(self, llm):
            self.llm = llm

    class _FakeProvider:
        def build_implementation_team_lead(self, team_kind, llm):
            return _FakeLead(llm)

    def _llm_getter(key: str) -> str:
        captured_keys.append(key)
        return f"{key}-client"

    worker = orch_mod._build_implementation_worker(
        "frontend_v2",
        StackSpec(name="frontend_v2", tools_services=["React"]),
        _llm_getter,
        _FakeProvider(),
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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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


def test_fresh_run_defaults_missing_task_id(tmp_path, monkeypatch):
    """Malformed Tech Lead task output without an id becomes a stable fallback task."""

    class StubTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"title": "Untitled task"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    captured: Dict[str, TaskGraphService] = {}

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            captured["graph"] = self.graph

        def run(self, **kw):
            self.graph.mark_branch_merged("task_1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: None,
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    task = captured["graph"].get_task("task_1")
    assert task.title == "Untitled task"
    assert task.status == TaskStatus.MERGED


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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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
        f"{GIT_UTILS}.merge_branch", lambda *a, **k: merge_calls.append(a) or (True, "ok")
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
        f"{GIT_UTILS}.abort_merge", lambda p, *a, **k: aborted.append(p) or (True, "aborted")
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


def test_escalate_done_merge_raises_marks_failed_not_merged(tmp_path, monkeypatch):
    """A merge_branch call that raises (rather than returning a clean failure) is treated exactly
    like a failed merge — FAILED, cascaded, and aborted — not left to propagate out of
    _escalate_to_tech_lead uncaught."""
    aborted: list[Any] = []
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: "real unmerged changes")

    def _raising_merge(*a, **k):
        raise RuntimeError("git merge blew up")

    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", _raising_merge)
    monkeypatch.setattr(
        f"{GIT_UTILS}.abort_merge", lambda p, *a, **k: aborted.append(p) or (True, "aborted")
    )
    swarm, graph = _make_swarm(
        tmp_path, StubTechLead(approved=False, adjudication_verdict="done"), [StubWorker("a1")]
    )
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", feature_branch="feature/t1")

    swarm._escalate_to_tech_lead(graph.get_task("t1"))

    assert graph.get_task("t1").status == TaskStatus.FAILED  # not merged
    assert aborted  # the half-applied merge was aborted before cascading


def test_escalate_to_tech_lead_serializes_concurrent_merges(tmp_path, monkeypatch):
    """Two tasks independently hitting the no-change cap with a 'done' verdict in the same round
    (see orchestrator.run's parallel_map fan-out over active workers) must not call
    merge_branch/abort_merge on the shared checkout (self.path) concurrently — a `git checkout` +
    `git merge` from one task racing another's on the same working directory/index could corrupt
    it. self._merge_lock must serialize the git-mutating span so at most one merge is ever in
    flight, mirroring the existing HITL-pause serialization (self._pause_lock)."""
    import threading
    import time

    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: "real unmerged changes")

    lock = threading.Lock()
    concurrency = {"current": 0, "max": 0}

    def _merge_branch(*a, **k):
        with lock:
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
        try:
            time.sleep(0.05)  # give a badly-serialized implementation a real chance to overlap
            return True, "ok"
        finally:
            with lock:
                concurrency["current"] -= 1

    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", _merge_branch)

    tech_lead = StubTechLead(approved=False, adjudication_verdict="done")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1"), StubWorker("a2")])
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t1", "a1")
    graph.assign_task_to_agent("t2", "a2")
    graph.update_task("t1", feature_branch="feature/t1")
    graph.update_task("t2", feature_branch="feature/t2")

    threads = [
        threading.Thread(target=swarm._escalate_to_tech_lead, args=(graph.get_task(tid),))
        for tid in ("t1", "t2")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert concurrency["max"] == 1  # never more than one merge in flight at once
    assert graph.get_task("t1").status == TaskStatus.MERGED
    assert graph.get_task("t2").status == TaskStatus.MERGED


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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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


def test_quality_gate_code_review_reports_live_progress(tmp_path):
    """The quality-gate code review must surface the agent's sub-step reports as
    status_text + structured current_activity (rising fraction), then clear the
    activity on completion so a stale sub-bar never lingers."""
    import types

    class _Provider:
        def run_build_verification(self, *a, **k):
            return types.SimpleNamespace(success=True, error="")

        def run_linting(self, *a, **k):
            return None

        def run_code_review(self, **kwargs):
            cb = kwargs.get("progress_callback")
            assert cb is not None, "orchestrator must pass a progress callback"
            cb("reviewing", "chunk 1/2: a.py", 0.3)
            cb("reviewing", "chunk 2/2: b.py", 0.7)
            cb("done", "approved=True, issues=0", 1.0)
            return types.SimpleNamespace(approved=True, issues=[])

    updates: List[Dict[str, Any]] = []
    swarm, graph = _make_real_swarm(tmp_path, _Provider())
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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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

    def flaky(agent, prompt, required_keys=None):
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
        lambda agent, prompt, required_keys=None: (_ for _ in ()).throw(RuntimeError("down")),
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
        lambda agent, prompt, required_keys=None: (_ for _ in ()).throw(LLMRateLimitError("429")),
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

    def ok(agent, prompt, required_keys=None):
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
        lambda agent, prompt, required_keys=None: {
            "approved": True,
            "reason": "ok",
            "requested_changes": [],
        },
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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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
        lambda agent_id, spec, llm_getter, engine_provider: StubWorker(agent_id),
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

    def _record(agent, prompt, required_keys=None):
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


def test_quality_gate_review_receives_user_decisions(tmp_path):
    """The per-task quality-gate code review is also told the user's settled decisions."""
    import types

    captured: Dict[str, Any] = {}

    class _Provider:
        def run_build_verification(self, *a, **k):
            return types.SimpleNamespace(success=True, error="")

        def run_linting(self, *a, **k):
            return None

        def run_code_review(self, **kw):
            captured["user_decisions"] = kw.get("user_decisions")
            return types.SimpleNamespace(approved=True, issues=[])

    swarm, graph = _make_real_swarm(tmp_path, _Provider())
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

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert captured["user_decisions"] == ["Which DB? → Postgres", "Use TLS? → Yes"]


# ----------------------------------------------------- concurrent review fan-out


def _seed_in_review(graph, workers) -> None:
    """Add one IN_REVIEW task per worker (title 'T{n}', assigned to the worker)."""
    for i, w in enumerate(workers, start=1):
        tid = f"t{i}"
        graph.add_task(tid, title=f"T{i}")
        graph.assign_task_to_agent(tid, w.agent_id)
        graph.set_task_in_review(tid)


class _PerTaskTechLead(StubTechLead):
    """Tech Lead returning a per-task-title review verdict; thread-safe call recording."""

    def __init__(self, verdicts: Dict[str, Dict[str, Any]]) -> None:
        super().__init__(approved=True)
        self._verdicts = verdicts
        self._lock = __import__("threading").Lock()
        self.progress_seen: List[Any] = []

    def run_code_review(
        self,
        task_title,
        task_description,
        acceptance_criteria,
        changes_summary,
        user_decisions=None,
        progress_callback=None,
    ):
        with self._lock:
            self.review_calls.append(task_title)
            self.progress_seen.append(progress_callback)
        return dict(self._verdicts[task_title])


def test_review_fanout_applies_mixed_verdicts(tmp_path, monkeypatch):
    """A multi-task review round fans out concurrently, then applies each verdict serially:
    approved → MERGED, substantive rejection → IN_PROGRESS (revision), infra error → FAILED."""
    _patch_git(monkeypatch)
    workers = [StubWorker("a1"), StubWorker("a2"), StubWorker("a3")]
    tech_lead = _PerTaskTechLead(
        {
            "T1": {"approved": True, "reason": "", "requested_changes": []},
            "T2": {"approved": False, "reason": "needs tests", "requested_changes": ["add tests"]},
            "T3": {"approved": False, "error": True, "reason": "context overflow"},
        }
    )
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    _seed_in_review(graph, workers)

    swarm._review_and_merge(lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.MERGED
    assert graph.get_task("t2").status == TaskStatus.IN_PROGRESS  # sent back for revision
    assert graph.get_task("t2").revision_feedback[-1]["reason"] == "needs tests"
    assert graph.get_task("t3").status == TaskStatus.FAILED  # infra error fails once
    assert sorted(tech_lead.review_calls) == ["T1", "T2", "T3"]  # each reviewed exactly once


def test_review_fanout_runs_concurrently(tmp_path, monkeypatch):
    """Reviews in a round overlap: a barrier that only releases when all N run at once must be
    crossed. A serial loop would break the barrier (timeout) and fail the tasks instead of merging."""
    import threading

    _patch_git(monkeypatch)
    workers = [StubWorker("a1"), StubWorker("a2"), StubWorker("a3")]
    barrier = threading.Barrier(len(workers), timeout=10)

    class _BarrierTechLead(StubTechLead):
        def run_code_review(
            self,
            task_title,
            task_description,
            acceptance_criteria,
            changes_summary,
            user_decisions=None,
            progress_callback=None,
        ):
            # Blocks until every concurrent review reaches here; serial execution never releases it.
            barrier.wait()
            return {"approved": True, "reason": "", "requested_changes": []}

    swarm, graph = _make_swarm(tmp_path, _BarrierTechLead(approved=True), workers)
    _seed_in_review(graph, workers)

    swarm._review_and_merge(lambda **kw: None)

    # All merged ⇒ every review crossed the barrier ⇒ the reviews genuinely ran concurrently.
    assert all(graph.get_task(f"t{i}").status == TaskStatus.MERGED for i in (1, 2, 3))


def test_review_fanout_suppresses_per_task_progress_bridge(tmp_path, monkeypatch):
    """In the concurrent path per-task progress bridges are suppressed (they would race the single
    job-record sub-bar); one aggregate status is emitted and run_code_review gets progress=None."""
    _patch_git(monkeypatch)
    workers = [StubWorker("a1"), StubWorker("a2")]
    tech_lead = _PerTaskTechLead(
        {
            "T1": {"approved": True, "reason": "", "requested_changes": []},
            "T2": {"approved": True, "reason": "", "requested_changes": []},
        }
    )
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    _seed_in_review(graph, workers)

    updates: List[Dict[str, Any]] = []
    swarm._review_and_merge(lambda **kw: updates.append(kw))

    assert all(cb is None for cb in tech_lead.progress_seen)  # no per-task bridge in fan-out
    assert any("reviewing 2 task(s)" in (u.get("status_text") or "") for u in updates)


def test_single_review_keeps_live_progress_bridge(tmp_path, monkeypatch):
    """The sole-review path still drives a live per-task ActivityBridge (progress callback present)."""
    _patch_git(monkeypatch)
    worker = StubWorker("a1")
    tech_lead = _PerTaskTechLead({"T1": {"approved": True, "reason": "", "requested_changes": []}})
    swarm, graph = _make_swarm(tmp_path, tech_lead, [worker])
    _seed_in_review(graph, [worker])

    swarm._review_and_merge(lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.MERGED
    assert tech_lead.progress_seen == [tech_lead.progress_seen[0]]
    assert tech_lead.progress_seen[0] is not None  # live bridge passed through


def test_review_fanout_exception_fails_only_that_task_once(tmp_path, monkeypatch):
    """An exception raised while reviewing one task is contained: that task fails once (error verdict)
    and the other tasks in the round still review and merge normally."""
    _patch_git(monkeypatch)
    workers = [StubWorker("a1"), StubWorker("a2")]

    class _BoomOnT2TechLead(StubTechLead):
        def __init__(self):
            super().__init__(approved=True)
            self.calls = __import__("collections").Counter()
            self._lock = __import__("threading").Lock()

        def run_code_review(
            self,
            task_title,
            task_description,
            acceptance_criteria,
            changes_summary,
            user_decisions=None,
            progress_callback=None,
        ):
            with self._lock:
                self.calls[task_title] += 1
            if task_title == "T2":
                raise RuntimeError("reviewer blew up")
            return {"approved": True, "reason": "", "requested_changes": []}

    tech_lead = _BoomOnT2TechLead()
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    _seed_in_review(graph, workers)

    swarm._review_and_merge(lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.MERGED
    assert graph.get_task("t2").status == TaskStatus.FAILED  # failed once, not looped
    assert tech_lead.calls["T2"] == 1


def test_review_concurrency_env_parsing(monkeypatch):
    """CODING_TEAM_REVIEW_CONCURRENCY: default when unset/garbage, floored at 1, honored otherwise."""
    monkeypatch.delenv("CODING_TEAM_REVIEW_CONCURRENCY", raising=False)
    assert orch_mod._review_concurrency() == orch_mod.REVIEW_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_REVIEW_CONCURRENCY", "not-a-number")
    assert orch_mod._review_concurrency() == orch_mod.REVIEW_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_REVIEW_CONCURRENCY", "0")
    assert orch_mod._review_concurrency() == 1  # floored so review always progresses
    monkeypatch.setenv("CODING_TEAM_REVIEW_CONCURRENCY", "7")
    assert orch_mod._review_concurrency() == 7


# --------------------------------------------------- implementation-worker fan-out (worktrees)


def test_implementation_fanout_runs_concurrently(tmp_path, monkeypatch):
    """Workers in a round overlap: a barrier that only releases when both run at once must be
    crossed. A serial loop would time out the barrier, the exception would be contained, and
    the tasks would end up bounced for revision instead of reaching review/merge."""
    import threading

    _patch_git(monkeypatch)
    barrier = threading.Barrier(2, timeout=10)

    class _BarrierWorker:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.stack_spec = StackSpec(name=agent_id, tools_services=[])

        def run_implement(self, task, path, repo_context=""):
            # Blocks until both workers reach here; serial execution never releases it.
            barrier.wait()
            return {
                "status": "in_review",
                "feature_branch": f"feature/{task.id}",
                "changes_summary": f"did {task.id}",
                "files_to_create_or_edit": [],
                "error": None,
            }

    workers = [_BarrierWorker("a1"), _BarrierWorker("a2")]
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), workers)
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t1", "a1")
    graph.assign_task_to_agent("t2", "a2")

    swarm.run(max_rounds=1)

    # Both merged in the same round ⇒ both crossed the barrier ⇒ implementation genuinely ran
    # concurrently (a serial loop would have left at least one task bounced for revision).
    assert graph.get_task("t1").status == TaskStatus.MERGED
    assert graph.get_task("t2").status == TaskStatus.MERGED


def test_implementation_fanout_serializes_hitl_pauses(tmp_path, monkeypatch):
    """Two workers escalating a decision in the same round never call pause_for_questions
    concurrently. The pause cycle stores exactly one outstanding question batch in job-level
    state (pending_questions/waiting_for_answers) — a second concurrent pause would overwrite
    the first's batch and a single answer submission would (mis)resolve both waiters. The lock
    around the pause call must serialize the round-trip so each escalation is fully posted,
    answered, and resolved before the next one starts."""
    import threading
    import time

    _patch_git(monkeypatch)

    class _DecisionWorker:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.stack_spec = StackSpec(name=agent_id, tools_services=[])

        def run_implement(self, task, path, repo_context=""):
            return {
                "status": "needs_decision",
                "open_questions": [{"question_text": f"Q for {task.id}?"}],
                "feature_branch": f"feature/{task.id}",
                "changes_summary": "",
                "error": None,
            }

    workers = [_DecisionWorker("a1"), _DecisionWorker("a2")]
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), workers)
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t1", "a1")
    graph.assign_task_to_agent("t2", "a2")

    lock = threading.Lock()
    concurrency = {"current": 0, "max": 0}

    def _pause_for_questions(questions, source):
        with lock:
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
        try:
            # Give a badly-serialized implementation a real chance to overlap.
            time.sleep(0.05)
            return [{"question_text": questions[0]["question_text"], "answer": "ok"}], True
        finally:
            with lock:
                concurrency["current"] -= 1

    swarm.run(max_rounds=1, pause_for_questions=_pause_for_questions)

    assert concurrency["max"] == 1  # never more than one pause round-trip in flight at once
    for tid in ("t1", "t2"):
        task = graph.get_task(tid)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.revision_feedback[-1]["source"] == "user_decision"


def test_implementation_fanout_uses_distinct_worktree_paths(tmp_path):
    """Each worker's run_implement receives its own worktree path — never self.path or another
    worker's path — proving branch/file isolation holds under the concurrent fan-out."""

    class _PathRecordingWorker:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.stack_spec = StackSpec(name=agent_id, tools_services=[])
            self.paths_seen: List[Any] = []

        def run_implement(self, task, path, repo_context=""):
            self.paths_seen.append(path)
            return {
                "status": "in_review",
                "feature_branch": f"feature/{task.id}",
                "changes_summary": f"did {task.id}",
                "files_to_create_or_edit": [],
                "error": None,
            }

    workers = [_PathRecordingWorker("a1"), _PathRecordingWorker("a2")]
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), workers)
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t1", "a1")
    graph.assign_task_to_agent("t2", "a2")

    swarm.run(max_rounds=1)

    path_a1 = workers[0].paths_seen[0]
    path_a2 = workers[1].paths_seen[0]
    assert path_a1 != path_a2
    assert path_a1 == swarm._worktrees.path_for("a1")
    assert path_a2 == swarm._worktrees.path_for("a2")
    assert path_a1 != swarm.path
    assert path_a2 != swarm.path


def test_implementation_fanout_exception_fails_only_that_task_once(tmp_path, monkeypatch):
    """An exception raised by one worker's run_implement is contained: that task is bounced for
    another attempt (not crashed/aborted), while the other worker's task still reaches review and
    merges normally in the same round."""
    _patch_git(monkeypatch)

    class _BoomWorker:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.stack_spec = StackSpec(name=agent_id, tools_services=[])
            self.calls = 0

        def run_implement(self, task, path, repo_context=""):
            self.calls += 1
            raise RuntimeError("implementation blew up")

    class _OkWorker:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.stack_spec = StackSpec(name=agent_id, tools_services=[])

        def run_implement(self, task, path, repo_context=""):
            return {
                "status": "in_review",
                "feature_branch": f"feature/{task.id}",
                "changes_summary": f"did {task.id}",
                "files_to_create_or_edit": [],
                "error": None,
            }

    boom = _BoomWorker("a1")
    ok = _OkWorker("a2")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [boom, ok])
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t1", "a1")
    graph.assign_task_to_agent("t2", "a2")

    swarm.run(max_rounds=1)

    # t1's worker crashed: bounced for another attempt, not aborted, and did not corrupt the round.
    assert graph.get_task("t1").status == TaskStatus.IN_PROGRESS
    assert graph.get_task("t1").revision_count == 1
    assert boom.calls == 1
    # t2's worker succeeded normally, reached review, and merged (Tech Lead approves).
    assert graph.get_task("t2").status == TaskStatus.MERGED


def test_implementation_fanout_suppresses_per_task_progress(tmp_path):
    """Concurrent workers don't emit per-task 'Implementing: ...' status text (it would race); one
    aggregate message is emitted before the round fans out instead."""
    workers = [StubWorker("a1"), StubWorker("a2")]
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), workers)
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t1", "a1")
    graph.assign_task_to_agent("t2", "a2")

    updates: List[Dict[str, Any]] = []
    swarm.run(max_rounds=1, update_fn=lambda **kw: updates.append(kw))

    per_task_texts = [
        u.get("status_text")
        for u in updates
        if (u.get("status_text") or "").startswith("Implementing: ")
    ]
    assert per_task_texts == []  # suppressed in the concurrent path
    assert any("Implementing 2 task(s)" in (u.get("status_text") or "") for u in updates)


def test_solo_worker_implementation_keeps_live_progress(tmp_path, monkeypatch):
    """With only one active worker this round, implementation still runs inline with live
    per-phase status text — unchanged from the pre-fanout behavior."""
    _patch_git(monkeypatch)
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")

    updates: List[Dict[str, Any]] = []
    swarm.run(max_rounds=1, update_fn=lambda **kw: updates.append(kw))

    assert any(u.get("status_text") == "Implementing: T1" for u in updates)
    assert graph.get_task("t1").status == TaskStatus.MERGED


def test_implementation_concurrency_env_parsing(monkeypatch):
    """CODING_TEAM_IMPLEMENTATION_CONCURRENCY: default when unset/garbage, floored at 1, honored
    otherwise."""
    monkeypatch.delenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", raising=False)
    assert orch_mod._implementation_concurrency() == orch_mod.IMPLEMENTATION_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", "not-a-number")
    assert orch_mod._implementation_concurrency() == orch_mod.IMPLEMENTATION_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", "0")
    assert (
        orch_mod._implementation_concurrency() == 1
    )  # floored so implementation always progresses
    monkeypatch.setenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", "7")
    assert orch_mod._implementation_concurrency() == 7


def test_run_cleans_up_worktrees_on_normal_completion(tmp_path, monkeypatch):
    _patch_git(monkeypatch)
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=5)

    assert graph.get_task("t1").status == TaskStatus.MERGED
    assert swarm._worktrees.cleanup_calls >= 1


def test_run_reports_failure_and_cleans_up_when_worktree_prepare_fails(tmp_path):
    """A WorktreeManager.prepare() failure fails the job cleanly (status=failed, aborted=True)
    instead of propagating an unhandled exception out of run(), and still cleans up."""
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")

    class _ExplodingWorktrees:
        def __init__(self):
            self.cleanup_calls = 0

        def prepare(self):
            raise RuntimeError("disk full")

        def path_for(self, agent_id):
            raise AssertionError("must not be reached")

        def cleanup(self):
            self.cleanup_calls += 1

    swarm._worktrees = _ExplodingWorktrees()
    updates: List[Dict[str, Any]] = []

    swarm.run(max_rounds=5, update_fn=lambda **kw: updates.append(kw))

    assert swarm.aborted is True
    assert updates[-1]["status"] == "failed"
    assert swarm._worktrees.cleanup_calls == 1
    assert graph.get_task("t1").status == TaskStatus.TO_DO  # never even attempted


def test_run_checks_cancellation_before_preparing_worktrees(tmp_path):
    """A job already cancelled before run() starts is honored immediately — it must not run
    (neither free nor guaranteed to succeed) worktree setup first, nor risk reporting
    status=failed instead of cancelled if that setup happens to error."""
    worker = StubWorker("a1")
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")

    class _PrepareCalledError(AssertionError):
        pass

    class _AssertNeverPreparedWorktrees:
        def prepare(self):
            raise _PrepareCalledError("prepare() must not be called when already cancelled")

        def path_for(self, agent_id):
            raise AssertionError("must not be reached")

        def cleanup(self):
            pass

    swarm._worktrees = _AssertNeverPreparedWorktrees()
    updates: List[Dict[str, Any]] = []

    swarm.run(
        max_rounds=5,
        check_cancel=lambda: True,
        update_fn=lambda **kw: updates.append(kw),
    )

    assert updates[-1]["status"] == "cancelled"
    assert swarm.aborted is False  # cancellation is not the same as an abort
    assert graph.get_task("t1").status == TaskStatus.TO_DO  # never even attempted


# ----------------------------------------------------- repo-context incremental cache


def test_repo_context_cache_matches_read_repo_context(tmp_path):
    """The cache renders the same briefing string as the full-read function for the same state."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.md").write_text("# doc")
    cache = orch_mod._RepoContextCache()
    assert cache.read(tmp_path) == orch_mod._read_repo_context(tmp_path)


def test_repo_context_cache_empty_repo(tmp_path):
    """An empty repo yields the sentinel, identical to _read_repo_context."""
    cache = orch_mod._RepoContextCache()
    assert cache.read(tmp_path) == "No files found"


def test_repo_context_cache_reuses_unchanged_rereads_changed(tmp_path, monkeypatch):
    """A second read re-renders only files whose (mtime, size) changed; unchanged files are reused."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 1")
    cache = orch_mod._RepoContextCache()
    first = cache.read(tmp_path)  # populates the cache (renders both)

    # Instrument renders that happen AFTER the cache is warm.
    rendered: List[str] = []
    real_render = orch_mod._render_context_file

    def _counting_render(f, repo_path):
        rendered.append(f.name)
        return real_render(f, repo_path)

    monkeypatch.setattr(orch_mod, "_render_context_file", _counting_render)

    second = cache.read(tmp_path)
    assert second == first
    assert rendered == []  # nothing changed → no file re-read

    # Change one file's content (and size) → only it is re-rendered on the next read.
    (tmp_path / "a.py").write_text("A = 222  # changed and longer")
    third = cache.read(tmp_path)
    assert rendered == ["a.py"]
    assert "A = 222" in third
    assert "B = 1" in third  # unchanged file still present, served from cache


def test_repo_context_cache_drops_removed_files(tmp_path):
    """A file removed between reads leaves the briefing and the internal cache."""
    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 1")
    cache = orch_mod._RepoContextCache()
    cache.read(tmp_path)

    (tmp_path / "b.py").unlink()
    out = cache.read(tmp_path)

    assert "A = 1" in out
    assert "B = 1" not in out
    assert all(p.name != "b.py" for p in cache._entries)


def test_repo_context_cache_reflects_new_files(tmp_path):
    """A file added between reads appears in the next briefing (mirrors the round-refresh contract)."""
    (tmp_path / "a.py").write_text("A = 1")
    cache = orch_mod._RepoContextCache()
    cache.read(tmp_path)

    (tmp_path / "notes.md").write_text("fresh notes")
    out = cache.read(tmp_path)

    assert "notes.md" in out
    assert "fresh notes" in out


def test_render_context_file_returns_none_on_read_error(tmp_path, monkeypatch):
    """A file that cannot be read renders to None (the caller then skips it)."""
    f = tmp_path / "a.py"
    f.write_text("A = 1")

    def _boom(self, *a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert orch_mod._render_context_file(f, tmp_path) is None


def test_enumerate_context_files_survives_walk_error(tmp_path, monkeypatch):
    """An os.walk failure is best-effort: it yields no files rather than raising."""
    monkeypatch.setattr(
        orch_mod.os, "walk", lambda *_a, **_k: (_ for _ in ()).throw(OSError("nope"))
    )
    assert orch_mod._enumerate_context_files(tmp_path) == []


def test_repo_context_cache_skips_unstattable_file(tmp_path, monkeypatch):
    """A file that cannot be stat-ed between walk and read is skipped (best-effort)."""
    monkeypatch.setattr(orch_mod, "_enumerate_context_files", lambda p: [tmp_path / "ghost.py"])
    cache = orch_mod._RepoContextCache()
    assert cache.read(tmp_path) == "No files found"  # ghost stat raises → skipped
    assert cache._entries == {}


def test_repo_context_cache_skips_unrenderable_file(tmp_path, monkeypatch):
    """A file whose render fails is skipped and not cached, even when its stat succeeds."""
    (tmp_path / "a.py").write_text("A = 1")
    monkeypatch.setattr(orch_mod, "_render_context_file", lambda f, root: None)
    cache = orch_mod._RepoContextCache()
    assert cache.read(tmp_path) == "No files found"
    assert cache._entries == {}


def test_review_uses_fresh_agent_per_call_not_shared(monkeypatch):
    """run_code_review must build a fresh review Agent per call (never reuse a shared instance), so
    concurrent reviews don't race on a Strands Agent's mutable conversation history."""
    from coding_team.tech_lead_agent import agent as tl_mod

    built = []

    class _FakeAgent:
        def __init__(self, **kw):
            built.append(self)

    monkeypatch.setattr(tl_mod, "Agent", _FakeAgent)
    monkeypatch.setattr("llm_service.util.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: {
            "approved": True,
            "reason": "",
            "requested_changes": [],
        },
    )
    tl = tl_mod.TechLeadAgent(model=object())
    built.clear()  # ignore the agents built in __init__; count only per-review construction

    used = []
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt, required_keys=None: (
            used.append(agent) or {"approved": True, "reason": "", "requested_changes": []}
        ),
    )

    tl.run_code_review("t", "d", [], "e1")
    tl.run_code_review("t", "d", [], "e2")

    assert len(built) == 2  # one fresh review agent per call
    assert used[0] is not used[1]  # the two reviews used distinct agent instances
    assert not hasattr(tl, "_review_agent")  # no shared review agent kept on the instance


def test_review_fanout_propagates_llm_attribution(tmp_path, monkeypatch):
    """The concurrent review workers must see the caller's LLM-attribution contextvar (parallel_map
    copies context into each worker); a raw thread pool would drop it and misattribute cost."""
    from llm_service import llm_attribution
    from llm_service.attribution import current_attribution

    _patch_git(monkeypatch)
    workers = [StubWorker("a1"), StubWorker("a2")]
    seen_team: List[str] = []
    lock = __import__("threading").Lock()

    class _AttrTechLead(StubTechLead):
        def run_code_review(
            self,
            task_title,
            task_description,
            acceptance_criteria,
            changes_summary,
            user_decisions=None,
            progress_callback=None,
        ):
            with lock:
                seen_team.append(current_attribution().team)
            return {"approved": True, "reason": "", "requested_changes": []}

    swarm, graph = _make_swarm(tmp_path, _AttrTechLead(approved=True), workers)
    _seed_in_review(graph, workers)

    with llm_attribution(team="coding_team_review"):
        swarm._review_and_merge(lambda **kw: None)

    assert seen_team == [
        "coding_team_review",
        "coding_team_review",
    ]  # attribution visible in workers


def test_single_review_exception_is_contained_and_fails_task_once(tmp_path, monkeypatch):
    """A sole review whose Tech Lead call raises is contained (converted to an error verdict) and
    fails just that task once, rather than propagating out of _review_and_merge and aborting."""
    _patch_git(monkeypatch)
    worker = StubWorker("a1")

    class _BoomTechLead(StubTechLead):
        def run_code_review(
            self,
            task_title,
            task_description,
            acceptance_criteria,
            changes_summary,
            user_decisions=None,
            progress_callback=None,
        ):
            raise RuntimeError("reviewer blew up")

    swarm, graph = _make_swarm(tmp_path, _BoomTechLead(approved=True), [worker])
    _seed_in_review(graph, [worker])

    swarm._review_and_merge(lambda **kw: None)  # must not raise

    assert graph.get_task("t1").status == TaskStatus.FAILED


# --------------------------------------------------------------------------- resume / retry_failed


def _seed_snapshot_with_failed_task() -> Dict[str, Any]:
    """Build a persisted job record (as a prior run leaves it) whose task graph has one FAILED task.

    The snapshot is produced from a real graph so its shape matches what ``graph.restore`` expects.
    """
    src = TaskGraphService(job_id="resume-job")
    src.add_task("t1", title="Backend task")
    src.update_task("t1", status=TaskStatus.FAILED)
    snap = src.snapshot()
    return {
        "repo_path": "/tmp/resume-repo",
        "task_graph_snapshot": snap["tasks"],
        "agent_task_map": snap["agent_task_map"],
        "stack_specs": orch_mod._DEFAULT_STACK_SPECS,
    }


def _run_resume_capturing_graph(tmp_path, monkeypatch, *, retry_failed: bool):
    """Drive run_coding_team_orchestrator down the resume branch with the swarm stubbed to a no-op,
    returning the graph it built so the test can inspect task statuses after resume handling."""
    record = _seed_snapshot_with_failed_task()
    record["repo_path"] = str(tmp_path)
    captured: Dict[str, Any] = {}

    class _StubSwarm:
        def __init__(self, *, graph, **kwargs):
            captured["graph"] = graph
            self.aborted = False

        def run(self, *args, **kwargs):
            return None

    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", _StubSwarm)
    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda *a, **k: object())
    monkeypatch.setattr(orch_mod, "_build_implementation_worker", lambda *a, **k: object())

    run_coding_team_orchestrator(
        "resume-job",
        str(tmp_path),
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: None,
        get_job_fn=lambda _jid: record,
        get_llm=lambda _key: object(),
        engine_provider=object(),
        retry_failed=retry_failed,
    )
    return captured["graph"]


def test_resume_retry_failed_true_demotes_failed(tmp_path, monkeypatch):
    """retry_failed=True demotes a snapshot's terminal FAILED task back to TO_DO on resume."""
    graph = _run_resume_capturing_graph(tmp_path, monkeypatch, retry_failed=True)
    assert graph.get_task("t1").status == TaskStatus.TO_DO


def test_resume_default_preserves_failed(tmp_path, monkeypatch):
    """The default resume (retry_failed=False) preserves a snapshot's FAILED task as terminal."""
    graph = _run_resume_capturing_graph(tmp_path, monkeypatch, retry_failed=False)
    assert graph.get_task("t1").status == TaskStatus.FAILED
