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
            self, task_title, task_description, acceptance_criteria, changes_summary
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

    class StubSWE:
        def __init__(self, *a, **k):
            self.agent_id = k.get("agent_id", "backend")

    captured: Dict[str, Any] = {}

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            captured["graph"] = self.graph

        def run(self, **kw):
            pass  # leave the restored state as-is so we can assert on it

    monkeypatch.setattr(orch_mod, "TechLeadAgent", ExplodingTL)
    monkeypatch.setattr(orch_mod, "SeniorSWEAgent", StubSWE)
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

    class StubSWE:
        def __init__(self, *a, **k):
            self.agent_id = k.get("agent_id", "backend")

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(orch_mod, "SeniorSWEAgent", StubSWE)
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

    class StubSWE:
        def __init__(self, *a, **k):
            self.agent_id = k.get("agent_id", "backend")

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(orch_mod, "SeniorSWEAgent", StubSWE)
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


# ----------------------------------------------------- implement passes inputs in full


def test_implement_passes_full_task_description_and_repo_context(tmp_path, monkeypatch):
    """A large task description and repo context reach the implement prompt in full — the
    engineer's inputs are never truncated."""
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
    huge = "X" * 60000
    big_ctx = "C" * 30000
    task = Task(id="t1", title="T1", description=huge)

    swe.run_implement(task, tmp_path, repo_context=big_ctx)

    assert huge in captured["prompt"]  # full description embedded, uncut
    assert big_ctx in captured["prompt"]  # full repo context embedded, uncut
