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

from shared.observability import bind_trace_id
from software_engineering_team import coding_team_orchestrator as orch_mod
from software_engineering_team import progress_config as progress_mod
from software_engineering_team.coding_team_orchestrator import (
    CodingTeamSwarm,
    run_coding_team_orchestrator,
)
from software_engineering_team.models import (
    CodingTeamPlanInput,
    StackSpec,
    Task,
    TaskStatus,
)
from software_engineering_team.shared.team_lead_base import TeamLeadSharedState
from software_engineering_team.task_graph import TaskGraphService
from software_engineering_team.team_routing import (
    _BACKEND_V2_STACK_SPEC,
    _DEVOPS_ROUTING_ENV,
    _DEVOPS_STACK_SPEC,
    _DEVOPS_TEAM_ALIASES,
    _quality_gate_agent_type,
    _target_matches_agent,
    _team_key,
    _v2_team_kind_for_stack,
)
from software_engineering_team.worker_factory import _v2_text_mode_llm

GIT_UTILS = "shared.git.git_utils"


# --------------------------------------------------------------------------- stubs


class _DefaultGroomTaskMixin:
    """Ungroomed-default ``run_groom_task`` for Tech-Lead stubs that don't care about grooming.

    Mirrors the real ``run_groom_task``'s own default/fallback shape, so mixing this in changes no
    existing assertion for a stub that doesn't otherwise override it.
    """

    def run_groom_task(
        self, task_id, task_title, task_description, task_dependencies, plan_context
    ):
        return {
            "acceptance_criteria": [],
            "out_of_scope": "",
            "description_enriched": task_description,
            "priority": "medium",
            "subtasks": [],
            "task_dependencies": task_dependencies,
        }


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
        self.spec_content_calls: List[str] = []
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
        spec_content="",
    ):
        self.review_calls.append(changes_summary)
        self.decision_calls.append(user_decisions)
        self.spec_content_calls.append(spec_content)
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

    def run_implement(self, task: Task, path) -> Dict[str, Any]:
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


def _make_swarm(tmp_path, tech_lead, workers, *, spec_content=""):
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=workers,
        graph=graph,
        path=Path(tmp_path),
        agent_ids=[w.agent_id for w in workers],
        llm_getter=lambda key: None,
        spec_content=spec_content,
    )
    # Real worktree creation is exercised in test_worktree_manager.py; give these
    # stub-worker tests a git-free stand-in instead (see _FakeWorktreeManager).
    swarm._worktrees = _FakeWorktreeManager(swarm.path, [w.agent_id for w in workers])
    swarm._worktrees.prepare()
    # Bypass the external quality-gate tools (build/lint/code-review) — not under test here.
    swarm._run_quality_gates = lambda *a, **k: True  # type: ignore[method-assign]
    return swarm, graph


def test_coding_team_swarm_is_team_lead_shared_state(tmp_path):
    """Preconditions: CodingTeamSwarm is constructed with a stub getter and empty workers.
    Postconditions: swarm is a TeamLeadSharedState; shared_config is {}; llm_getter identity
      is preserved; _status_callback defaults to None.
    """

    def getter(key: str):
        return None

    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[],
        graph=TaskGraphService(job_id="j1"),
        path=Path(tmp_path),
        agent_ids=[],
        llm_getter=getter,
    )
    assert isinstance(swarm, TeamLeadSharedState)
    assert swarm.llm_getter is getter
    assert swarm.shared_config == {}
    assert swarm._status_callback is None


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


@pytest.mark.parametrize(
    "progress_base, progress_span",
    [
        (-1, 10),  # negative base
        (10, -1),  # negative span
        (60, 50),  # sum exceeds 100
    ],
)
def test_invalid_progress_bounds_raise_value_error_not_assert(
    tmp_path, progress_base, progress_span
):
    """The progress_base/progress_span precondition raises ValueError, surviving `python -O`."""
    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    with pytest.raises(ValueError):
        run_coding_team_orchestrator(
            "j1",
            tmp_path,
            plan,
            update_job_fn=lambda **kw: None,
            get_job_fn=lambda jid: {},
            cache_dir=tmp_path,
            get_llm=lambda key: None,
            progress_base=progress_base,
            progress_span=progress_span,
        )


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
            spec_content="",
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


# ----------------------------------------------------- review-verdict cache (_review_verdict_cache)


def test_identical_diff_reuses_cached_verdict_without_second_review_call(tmp_path, monkeypatch):
    """A task reviewed twice with a byte-identical diff only pays for one Tech Lead call."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 20)
    _patch_git(monkeypatch, diff="same diff every time")
    tech_lead = StubTechLead(approved=False, reason="needs work", requested_changes=["fix X"])
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)
    assert len(tech_lead.review_calls) == 1
    first_feedback = graph.get_task("t1").revision_feedback[-1]

    graph.set_task_in_review("t1")  # simulate the task coming back into review with no changes
    swarm._review_and_merge(lambda **kw: None)

    assert len(tech_lead.review_calls) == 1  # second round reused the cached verdict
    second_feedback = graph.get_task("t1").revision_feedback[-1]
    assert second_feedback["reason"] == first_feedback["reason"] == "needs work"
    assert second_feedback["requested_changes"] == ["fix X"]


def test_changed_diff_triggers_fresh_review_call(tmp_path, monkeypatch):
    """A task reviewed twice with a genuinely different diff is reviewed both times."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 20)
    diffs = iter(["diff-round-1", "diff-round-2"])
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: next(diffs))
    tech_lead = StubTechLead(approved=False)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)
    assert len(tech_lead.review_calls) == 1

    graph.set_task_in_review("t1")
    swarm._review_and_merge(lambda **kw: None)

    assert len(tech_lead.review_calls) == 2  # different diff each round -> reviewed both times


def test_errored_review_is_never_cached(tmp_path, monkeypatch):
    """An ``error`` verdict is never cached — the next call for the same diff retries for real."""
    _patch_git(monkeypatch, diff="same diff")

    class FlakyTechLead(StubTechLead):
        def run_code_review(self, **kw):
            self.review_calls.append(kw.get("changes_summary", ""))
            if len(self.review_calls) == 1:
                return {
                    "approved": False,
                    "error": True,
                    "reason": "transient",
                    "requested_changes": [],
                }
            return {
                "approved": False,
                "error": False,
                "reason": "reject",
                "requested_changes": ["y"],
            }

    tech_lead = FlakyTechLead(approved=False)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")
    task = graph.get_task("t1")

    _, review1 = swarm._compute_review(task)
    assert review1["error"] is True

    _, review2 = swarm._compute_review(task)
    assert review2["error"] is False
    assert review2["reason"] == "reject"
    assert len(tech_lead.review_calls) == 2  # the errored call was never cached, so it retried

    # A third call with the same diff now hits the cache seeded by the second (non-error) call.
    _, review3 = swarm._compute_review(task)
    assert review3["reason"] == "reject"
    assert len(tech_lead.review_calls) == 2


def test_approved_verdict_is_also_cached(tmp_path, monkeypatch):
    """An approved verdict is cached too — only ``error`` verdicts are excluded."""
    _patch_git(monkeypatch, diff="same diff")
    tech_lead = StubTechLead(approved=True, reason="looks good")
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")
    task = graph.get_task("t1")

    swarm._compute_review(task)
    swarm._compute_review(task)

    assert len(tech_lead.review_calls) == 1


def test_cached_review_verdict_is_an_independent_copy(tmp_path, monkeypatch):
    """A cache hit returns its own copy — mutating it never corrupts the cached entry."""
    _patch_git(monkeypatch, diff="same diff")
    tech_lead = StubTechLead(approved=False, requested_changes=["fix X"])
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")
    task = graph.get_task("t1")

    _, review1 = swarm._compute_review(task)
    review1["requested_changes"].append("mutated!")  # mutate the caller's copy

    _, review2 = swarm._compute_review(task)  # cache hit
    assert review2["requested_changes"] == ["fix X"]  # unaffected by the mutation above
    assert len(tech_lead.review_calls) == 1


def test_review_verdict_cache_is_scoped_per_task(tmp_path, monkeypatch):
    """Two different tasks with byte-identical diffs are each reviewed once — no cross-task reuse."""
    _patch_git(monkeypatch, diff="same diff for both tasks")
    tech_lead = StubTechLead(approved=False)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1"), StubWorker("a2")])
    graph.add_task("t1", title="T1")
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t1", "a1")
    graph.assign_task_to_agent("t2", "a2")
    graph.set_task_in_review("t1")
    graph.set_task_in_review("t2")

    swarm._compute_review(graph.get_task("t1"))
    swarm._compute_review(graph.get_task("t2"))

    assert len(tech_lead.review_calls) == 2  # each task's own first review, not shared


def test_new_user_decision_invalidates_cache_even_with_unchanged_diff(tmp_path, monkeypatch):
    """A HITL decision answered mid-loop must reach the reviewer even when the branch
    diff hasn't changed since the last cached verdict.

    Regression test: the verdict cache previously keyed on the branch digest alone, so
    a newly answered decision (which _escalate_decision appends to revision_feedback
    without necessarily changing the branch) was silently invisible to a cache hit.
    """
    _patch_git(monkeypatch, diff="same diff")
    tech_lead = StubTechLead(approved=False)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")
    task = graph.get_task("t1")

    swarm._compute_review(task)
    assert len(tech_lead.review_calls) == 1
    assert tech_lead.decision_calls[-1] == []  # no decisions yet

    # Simulate a HITL decision answered mid-loop, in the same shape _escalate_decision
    # (swarm_implementation.py) appends — without driving the full pause machinery.
    graph.update_task(
        "t1",
        revision_feedback=list(task.revision_feedback or [])
        + [
            {
                "source": "user_decision",
                "reason": "Q? → A",
                "requested_changes": [],
                "decisions": [{"question": "Q?", "answer": "A"}],
            }
        ],
    )

    swarm._compute_review(graph.get_task("t1"))

    assert len(tech_lead.review_calls) == 2  # new decision -> cache correctly missed
    assert tech_lead.decision_calls[-1] == ["Q? → A"]


def test_changed_summary_invalidates_cache_even_with_unchanged_diff(tmp_path, monkeypatch):
    """A new changes_summary must reach the reviewer even when the branch diff hasn't
    changed since the last cached verdict.

    Regression test: _implement_and_verify unconditionally overwrites task.changes_summary
    on every run_implement call, whether or not the resulting diff actually changed — so an
    ordinary revision round can change the reviewer's evidence text without moving the
    branch digest at all.
    """
    _patch_git(monkeypatch, diff="same diff")
    tech_lead = StubTechLead(approved=False)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.update_task("t1", changes_summary="first summary")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._compute_review(graph.get_task("t1"))
    assert len(tech_lead.review_calls) == 1
    assert "first summary" in tech_lead.review_calls[-1]

    graph.update_task("t1", changes_summary="second summary")
    swarm._compute_review(graph.get_task("t1"))

    assert len(tech_lead.review_calls) == 2  # changed summary -> cache correctly missed
    assert "second summary" in tech_lead.review_calls[-1]
    assert "first summary" not in tech_lead.review_calls[-1]


def test_changed_acceptance_criteria_invalidates_cache_even_with_unchanged_diff(
    tmp_path, monkeypatch
):
    """Updated acceptance criteria must reach the reviewer even when the branch diff,
    changes_summary, and decisions are all unchanged.

    Regression test: TaskGraphService.update_task supports updating acceptance_criteria
    on an in-flight task (e.g. a scope change); the cache previously covered only the
    changes-summary/diff evidence and user decisions, so this went unnoticed.
    """
    _patch_git(monkeypatch, diff="same diff")
    tech_lead = StubTechLead(approved=False)
    swarm, graph = _make_swarm(tmp_path, tech_lead, [StubWorker("a1")])
    graph.add_task("t1", title="T1", acceptance_criteria=["first criterion"])
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._compute_review(graph.get_task("t1"))
    assert len(tech_lead.review_calls) == 1

    graph.update_task("t1", acceptance_criteria=["second criterion"])
    swarm._compute_review(graph.get_task("t1"))

    assert len(tech_lead.review_calls) == 2  # changed criteria -> cache correctly missed


def test_review_verdict_cache_key_covers_every_reviewer_input():
    """The cache key changes when any of run_code_review's six inputs changes —
    task title/description/acceptance criteria and spec_content, not just the
    changes-summary/diff evidence and user decisions (spec_content is swarm-level
    and never actually varies within one run, so this is exercised directly here
    rather than through an end-to-end swarm scenario)."""
    from software_engineering_team.swarm_review import _review_verdict_cache_key

    base = dict(
        task_title="T",
        task_description="D",
        acceptance_criteria=["a", "b"],
        evidence="ev",
        user_decisions=["Q -> A"],
        spec_content="spec",
    )
    baseline = _review_verdict_cache_key(**base)
    assert _review_verdict_cache_key(**base) == baseline  # deterministic

    for field, new_value in [
        ("task_title", "T2"),
        ("task_description", "D2"),
        ("acceptance_criteria", ["a", "c"]),
        ("evidence", "ev2"),
        ("user_decisions", ["Q -> A2"]),
        ("spec_content", "spec2"),
    ]:
        variant = dict(base, **{field: new_value})
        assert _review_verdict_cache_key(**variant) != baseline, (
            f"changing {field} did not invalidate the cache key"
        )


def test_review_verdict_cache_key_does_not_collide_across_list_boundaries():
    """Shifting an element between acceptance_criteria and user_decisions (the
    two variable-length lists sandwiched around evidence) must not produce the
    same key.

    Regression test: a flat separator-joined encoding cannot tell where one
    variable-length list ends and the next begins, so
    acceptance_criteria=["a", "b"], evidence="c", user_decisions=[] and
    acceptance_criteria=["a"], evidence="b", user_decisions=["c"] previously
    flattened to an identical sequence and collided.
    """
    from software_engineering_team.swarm_review import _review_verdict_cache_key

    base = dict(task_title="T", task_description="D", spec_content="spec")

    key_a = _review_verdict_cache_key(
        acceptance_criteria=["a", "b"], evidence="c", user_decisions=[], **base
    )
    key_b = _review_verdict_cache_key(
        acceptance_criteria=["a"], evidence="b", user_decisions=["c"], **base
    )

    assert key_a != key_b


# ----------------------------------------------------- review retry / failure handling


def test_review_retries_transient_error_then_succeeds(monkeypatch):
    """A transient reviewer error (rate limit/timeout) is retried, not turned into a rejection."""
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
        def run_implement(self, task, path):
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


def test_approved_merge_conflict_sends_back_for_revision(tmp_path, monkeypatch):
    """An approved task whose merge_branch call returns (False, ...) without raising (e.g. a
    conflict) must not be left silently stuck IN_REVIEW — it should bounce through the same
    _request_revision path an unapproved review takes, with the merge failure recorded."""
    _patch_git(monkeypatch, merge=(False, "merge conflict"))
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [StubWorker("a1")])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", feature_branch="feature/t1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.IN_PROGRESS  # bounced, not stuck IN_REVIEW
    assert task.revision_count == 1
    assert "merge conflict" in task.revision_feedback[-1]["reason"]


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


def test_tech_lead_review_receives_spec_content(tmp_path, monkeypatch):
    """The swarm's plan-level spec content reaches the Tech Lead's review — this is now the
    swarm's sole code-review call (the quality gate's duplicate review was removed), so it is
    the only place spec constraints outside a task's own description/acceptance criteria can be
    checked."""
    _patch_git(monkeypatch)
    tech_lead = StubTechLead(approved=True)
    swarm, graph = _make_swarm(
        tmp_path, tech_lead, [StubWorker("a1")], spec_content="THE FULL PROJECT SPEC"
    )
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    assert tech_lead.spec_content_calls == ["THE FULL PROJECT SPEC"]


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
    from shared.git.git_utils import (
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
    from shared.git.git_utils import branch_diff

    assert branch_diff(tmp_path / "does-not-exist", "development", "feature/x") == ""


def test_branch_diff_bad_branch_returns_empty(tmp_path):
    """A failing git diff (e.g. unknown branch) yields "" rather than raising."""
    from shared.git.git_utils import branch_diff, initialize_new_repo

    ok, _ = initialize_new_repo(tmp_path)
    assert ok
    assert branch_diff(tmp_path, "development", "feature/does-not-exist") == ""


def test_status_text_reports_merged_and_failed_counts(tmp_path, monkeypatch):
    class StubTL(_DefaultGroomTaskMixin):
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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


def test_terminal_status_write_survives_slow_pending_graph_persist(tmp_path, monkeypatch):
    """A graph mutation's background persist write must never land AFTER — and clobber —
    the terminal status write. Task-graph mutators (e.g. mark_branch_merged) enqueue their
    job-service write into a background flusher (see _persist_graph_async) rather than
    writing synchronously while TaskGraphService's lock is held; every _update() call —
    including the final terminal status write — must drain that flusher first, so a
    still-in-flight write can never complete after and overwrite fresher phase/status_text/
    progress with stale data. Simulates a slow job-service write for any call carrying a
    task_graph_snapshot (the background-persist payload) and asserts the terminal write is
    always the last one recorded."""
    import threading
    import time

    class StubTL(_DefaultGroomTaskMixin):
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
            # Triggers the async persist_callback path (invoked while TaskGraphService's
            # lock is held) — enqueues a slow-to-write payload in the background and
            # returns immediately, well before the terminal write below.
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _update_job_fn(**kw: Any) -> None:
        if "task_graph_snapshot" in kw:
            time.sleep(0.1)  # simulate a slow background job-service write
        with lock:
            updates.append(kw)

    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=_update_job_fn,
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert updates, "orchestrator must write at least the terminal status"
    final = updates[-1]
    assert final.get("status") == "completed"
    assert final.get("progress") == 100
    # No write after the terminal one may carry a task_graph_snapshot: that would mean a
    # stale background write landed after — and could have clobbered — the terminal fields.
    assert "task_graph_snapshot" not in final


def test_failed_background_persist_write_is_retried_at_round_boundary(tmp_path, monkeypatch):
    """If the background write for a graph mutation fails, _persist_state must NOT have
    already been marked as delivered — otherwise the round-boundary sync checkpoint
    (_persist_graph_sync) sees no change and skips retrying, permanently leaving the
    terminal job's snapshot stale even though later status writes succeed. Fails the first
    job-service write that carries a task_graph_snapshot and asserts the retried write
    still lands with the correct (merged) state."""

    class StubTL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    class StubSwarm:
        aborted = False

        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            # Triggers the async persist_callback path, then simulates the round-boundary
            # durability checkpoint the real swarm loop performs every round.
            self.graph.mark_branch_merged("t1")
            kw["persist_fn"]()

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []
    failed_once = {"done": False}

    def _update_job_fn(**kw: Any) -> None:
        snap = kw.get("task_graph_snapshot")
        # Target specifically the write for the merge mutation (not the earlier add_task /
        # pre-loop writes, whose snapshot has no merged task yet) — this is what the review
        # comment described: a failure on the FINAL mutation's write, with no later mutation
        # around to incidentally paper over it.
        if snap and any(t.get("status") == "merged" for t in snap) and not failed_once["done"]:
            failed_once["done"] = True
            raise RuntimeError("job service unavailable")
        updates.append(kw)

    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=_update_job_fn,
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert failed_once["done"], "the merge write must have been attempted and failed once"
    snapshot_writes = [kw for kw in updates if "task_graph_snapshot" in kw]
    merged_writes = [
        kw
        for kw in snapshot_writes
        if any(t["status"] == "merged" for t in kw["task_graph_snapshot"])
    ]
    assert merged_writes, (
        "the merged state must have been retried and successfully delivered — a failed "
        "write must not be silently mistaken for a delivered one"
    )


def test_status_write_survives_concurrent_worker_graph_mutation(tmp_path, monkeypatch):
    """A status write (e.g. a HITL pause to waiting_for_user) racing a DIFFERENT worker's
    concurrent graph mutation must never be clobbered by that mutation's background write
    landing after it — even though the mutation's own graph.update_task()/mark_branch_merged()
    call returns immediately (never blocked by the status write), its resulting background
    write must still be ordered to land AFTER, not before, the status write. Mirrors the
    real implementation fan-out: one worker escalates a decision (publishing a status write
    via _update) while another is still mutating the graph in the same round."""
    import threading
    import time

    class StubTL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    pause_write_started = threading.Event()
    mutation_enqueued = threading.Event()

    class StubSwarm:
        aborted = False

        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            update_fn = kw["update_fn"]

            def _mutate_concurrently() -> None:
                # Simulates a second worker, still active in the same fan-out, mutating
                # the graph (and thus enqueuing a background write) while the first
                # worker's status write below is in flight.
                pause_write_started.wait(timeout=5)
                self.graph.mark_branch_merged("t1")
                mutation_enqueued.set()

            t = threading.Thread(target=_mutate_concurrently)
            t.start()
            update_fn(
                status="waiting_for_user",
                phase="waiting",
                status_text="Paused for user input",
            )
            t.join(timeout=5)

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _update_job_fn(**kw: Any) -> None:
        if kw.get("status") == "waiting_for_user":
            pause_write_started.set()
            # Hold this write "in flight" until the concurrent mutation has enqueued its
            # background write, then a bit longer — giving that background write every
            # chance to race ahead if the ordering guarantee were broken.
            mutation_enqueued.wait(timeout=5)
            time.sleep(0.05)
        with lock:
            updates.append(kw)

    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=_update_job_fn,
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    pause_idx = next(i for i, kw in enumerate(updates) if kw.get("status") == "waiting_for_user")
    mutation_write_indices = [
        i
        for i, kw in enumerate(updates)
        if "task_graph_snapshot" in kw
        and any(t.get("status") == "merged" for t in kw["task_graph_snapshot"])
    ]
    assert mutation_write_indices, "the concurrent mutation's write must eventually land"
    assert mutation_write_indices[0] > pause_idx, (
        "the concurrently-enqueued mutation write landed before (or during) the pause "
        "write instead of strictly after it — a stale background write could clobber a "
        "fresher direct status write"
    )


def test_background_graph_write_after_pause_carries_pause_phase_not_stale_coding_phase(
    tmp_path, monkeypatch
):
    """Same race as test_status_write_survives_concurrent_worker_graph_mutation, but checking
    the LANDED CONTENT rather than just write order: the concurrently-enqueued mutation's
    background write — which write_now() deliberately lets land AFTER the pause's direct write,
    not before — must itself carry the pause's phase="waiting"/status_text, never the stale
    phase="coding"/status_text="Assigning and implementing tasks" that was current when the
    graph mutation triggered the persist callback. A prior implementation baked phase/
    status_text into the background payload at enqueue time; since that write is guaranteed to
    land after the pause write (not before, so drain-based ordering doesn't save it either), the
    stale value would win via the job service's shallow merge — silently un-pausing the job's
    displayed phase/status_text even though status/waiting_for_answers stayed correctly paused."""
    import threading
    import time

    class StubTL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    pause_write_started = threading.Event()
    mutation_enqueued = threading.Event()

    class StubSwarm:
        aborted = False

        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            update_fn = kw["update_fn"]

            def _mutate_concurrently() -> None:
                pause_write_started.wait(timeout=5)
                self.graph.mark_branch_merged("t1")
                mutation_enqueued.set()

            t = threading.Thread(target=_mutate_concurrently)
            t.start()
            update_fn(
                status="waiting_for_user",
                phase="waiting",
                status_text="Paused for user input",
            )
            t.join(timeout=5)

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _update_job_fn(**kw: Any) -> None:
        if kw.get("status") == "waiting_for_user":
            pause_write_started.set()
            mutation_enqueued.wait(timeout=5)
            time.sleep(0.05)
        with lock:
            updates.append(kw)

    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=_update_job_fn,
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    mutation_write_indices = [
        i
        for i, kw in enumerate(updates)
        if "task_graph_snapshot" in kw
        and any(t.get("status") == "merged" for t in kw["task_graph_snapshot"])
    ]
    assert mutation_write_indices, "the concurrent mutation's write must eventually land"
    mutation_write = updates[mutation_write_indices[0]]
    assert mutation_write.get("phase") == "waiting", (
        f"background graph write regressed phase to a stale value: {mutation_write.get('phase')!r}"
    )
    assert mutation_write.get("status_text") == "Paused for user input", (
        "background graph write regressed status_text to a stale value: "
        f"{mutation_write.get('status_text')!r}"
    )


def test_failed_direct_write_does_not_leak_into_background_graph_persist(tmp_path, monkeypatch):
    """When _raw_update raises for a phase-changing _update() call (e.g. a pause write whose
    job-service PATCH fails), the outer phase/status_text must NOT be updated to the failed
    call's values. A subsequent background graph persist reads phase/status_text live (see
    _persist_graph_async) — if the failed call had already committed its new values before
    _raw_update raised, that background write would publish a phase/status_text that was never
    actually confirmed on the wire, even though the rest of the failed call's fields (e.g. HITL
    pending-question metadata on a real pause) never landed either."""

    class StubTL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    class StubSwarm:
        aborted = False

        def __init__(self, *a, **k):
            self.graph = k["graph"]

        def run(self, **kw):
            update_fn = kw["update_fn"]
            try:
                update_fn(
                    status="waiting_for_user",
                    phase="waiting",
                    status_text="Paused for user input",
                )
            except RuntimeError:
                pass  # simulates a caller (e.g. pause_cycle) surviving a failed publish
            self.graph.mark_branch_merged("t1")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    updates: List[Dict[str, Any]] = []

    def _update_job_fn(**kw: Any) -> None:
        if kw.get("status") == "waiting_for_user":
            raise RuntimeError("job service unavailable")
        updates.append(kw)

    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=_update_job_fn,
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    mutation_write_indices = [
        i
        for i, kw in enumerate(updates)
        if "task_graph_snapshot" in kw
        and any(t.get("status") == "merged" for t in kw["task_graph_snapshot"])
    ]
    assert mutation_write_indices, "the graph mutation's write must eventually land"
    mutation_write = updates[mutation_write_indices[0]]
    assert mutation_write.get("phase") != "waiting", (
        "background graph write leaked the failed direct write's phase into live state: "
        f"{mutation_write.get('phase')!r}"
    )
    assert mutation_write.get("status_text") != "Paused for user input", (
        "background graph write leaked the failed direct write's status_text into live state: "
        f"{mutation_write.get('status_text')!r}"
    )


# ----------------------------------------------------- real quality-gate path


def _gate_provider(*, build_ok=True, build_raises=False, lint_ok=True, lint_raises=False):
    """A fake CodeEngineProvider exposing just the quality-gate methods the swarm calls."""
    import types

    class _FakeGateProvider:
        def run_build_verification(self, *a, **k):
            if build_raises:
                raise RuntimeError("tool crashed")
            return types.SimpleNamespace(success=build_ok, error="" if build_ok else "boom build")

        def run_linting(self, *a, **k):
            if lint_raises:
                raise RuntimeError("lint tool crashed")
            if lint_ok:
                return types.SimpleNamespace(passed=True, issues=[])
            return types.SimpleNamespace(
                passed=False, issues=[{"message": "line too long", "file_path": "x.py"}]
            )

    return _FakeGateProvider()


def _make_real_swarm(tmp_path, provider):
    """A swarm WITHOUT the _run_quality_gates bypass, with one task already assigned to a1.

    ``provider`` supplies the build/lint engines (see ``_gate_provider``).
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
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=True))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


def test_quality_gates_skip_with_warning_when_no_engine_provider(tmp_path):
    """No engine_provider configured (an embedder wired the swarm directly, without injecting
    build/lint engines) → gates are skipped, not silently: a SKIPPED status is reported
    and the task still proceeds straight to review."""
    swarm, graph = _make_real_swarm(tmp_path, None)

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


def test_quality_gates_skipped_for_devops_worker(tmp_path):
    """DevOps runs its own internal gates; the generic build/lint gate must never
    touch a devops worker's output -- prove it by making the provider raise if called."""

    class _RaisingProvider:
        def run_build_verification(self, *a, **k):
            raise AssertionError("build verification must not run for a devops worker")

        def run_linting(self, *a, **k):
            raise AssertionError("linting must not run for a devops worker")

    devops_worker = StubWorker("devops_worker")
    devops_worker.team_kind = "devops"
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[devops_worker],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["devops_worker"],
        llm_getter=lambda k: None,
        engine_provider=_RaisingProvider(),
    )
    swarm._worktrees = _FakeWorktreeManager(swarm.path, ["devops_worker"])
    swarm._worktrees.prepare()
    graph.add_task("t1", title="T1", target_team="devops")
    graph.assign_task_to_agent("t1", "devops_worker")

    ok = swarm._run_quality_gates(
        devops_worker, graph.get_task("t1"), lambda **kw: None, worktree_path=Path(tmp_path)
    )

    assert ok is True


def test_quality_gates_run_normally_for_worker_without_team_kind_attr(tmp_path):
    """A worker with no team_kind attribute at all (e.g. a minimal duck-typed stub)
    still runs the normal gate path -- getattr defaults to None, not "devops"."""
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=True))
    assert not hasattr(swarm.workers[0], "team_kind")

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    assert graph.get_task("t1").status == TaskStatus.IN_REVIEW


def test_quality_gate_build_failure_returns_for_revision(tmp_path):
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=False))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO  # returned for revision
    assert task.assigned_agent_id is None  # and unassigned


def test_quality_gate_lint_failure_returns_for_revision(tmp_path):
    """A failing lint result (build OK) must not be silently ignored — the task is bounced for
    revision with the lint issues recorded, exactly like a build failure."""
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=True, lint_ok=False))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO  # returned for revision, not merged silently
    assert task.assigned_agent_id is None
    assert any(
        e.get("type") == "lint" and "line too long" in e.get("error", "")
        for e in task.revision_feedback or []
    )


def test_quality_gate_build_tool_exception_returns_for_revision(tmp_path):
    """An unexpected build-tool error is logged AND the gate is failed (not silently passed) —
    a crashed tool means the gate never actually ran, so unverified code must not proceed."""
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_raises=True))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO  # returned for revision, not merged silently
    assert task.assigned_agent_id is None
    assert any(e.get("type") == "tool_error" for e in task.revision_feedback or [])


def test_quality_gate_lint_tool_exception_returns_for_revision(tmp_path):
    """Build succeeds but linting raises — the gate must still fail closed, matching the
    build-tool-exception case, rather than falling through to a silent pass."""
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_ok=True, lint_raises=True))

    swarm._implement_and_verify(swarm.workers[0], lambda **kw: None)

    task = graph.get_task("t1")
    assert task.status == TaskStatus.TO_DO  # returned for revision, not merged silently
    assert task.assigned_agent_id is None
    assert any(e.get("type") == "tool_error" for e in task.revision_feedback or [])


def test_quality_gate_tool_exception_logs_full_traceback(tmp_path, caplog):
    """An unexpected quality-gate tool error must be logged WITH a full traceback
    (logger.exception → ERROR + exc_info), not a one-line WARNING — a silent
    summary is exactly what made the review-phase crash undebuggable."""
    import logging as _logging

    # build() raises RuntimeError("tool crashed")
    swarm, graph = _make_real_swarm(tmp_path, _gate_provider(build_raises=True))

    with caplog.at_level(
        _logging.ERROR, logger="software_engineering_team.coding_team_orchestrator"
    ):
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
    """Backend-owned target aliases such as devops route to the backend v2 worker.

    This documents CODING_TEAM_DEVOPS_ROUTING's default-OFF behavior — see
    test_devops_aliases_route_to_devops_when_flag_on for the flag-enabled case.
    """

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
    assert _team_key("ui") == "frontend_v2"
    assert _team_key("UX") == "frontend_v2"
    assert _team_key("Web App") == "frontend_v2"
    assert _target_matches_agent("ui", "frontend-v2-worker-2") is True
    assert _target_matches_agent("ux", "backend_v2_worker_1") is False
    # Exact-token match only: unrelated words containing an alias substring are unaffected.
    assert _team_key("build") == "build"
    assert _team_key("guidelines") == "guidelines"


def test_team_key_routes_framework_and_language_labels() -> None:
    """Concrete tech labels route to the owning v2 team instead of failing to match."""
    for label in ("React", "Angular", "AngularJS", "scss", "Next.js", "React.js", "Vue.js"):
        assert _team_key(label) == "frontend_v2"
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
        assert _team_key(label) == "backend_v2"
    # Ambiguous languages (used by frontend frameworks AND Node backends) must NOT be
    # forced onto a team — routing them would mis-send backend work to the frontend worker.
    for label in ("TypeScript", "JavaScript"):
        assert _team_key(label) not in ("frontend_v2", "backend_v2")
    # A capable worker now matches these labels rather than the task being dropped.
    assert _target_matches_agent("react", "frontend_v2") is True
    assert _target_matches_agent("python", "backend_v2") is True
    # Generic, non-tech words still pass through unmapped.
    assert _team_key("build") == "build"


def test_team_key_accepts_compact_v2_labels() -> None:
    """Separator-less v2 labels still route, without matching unrelated substrings."""
    assert _team_key("frontendv2") == "frontend_v2"
    assert _team_key("BackendV2") == "backend_v2"
    # Exact-match only: a word merely containing the alias as a substring is unaffected.
    assert _team_key("myfrontend") == "myfrontend"


# ----------------------------------------------------- CODING_TEAM_DEVOPS_ROUTING


@pytest.mark.parametrize("alias", sorted(_DEVOPS_TEAM_ALIASES))
def test_devops_aliases_stay_backend_when_flag_off(monkeypatch, alias) -> None:
    monkeypatch.delenv(_DEVOPS_ROUTING_ENV, raising=False)
    assert _team_key(alias) == "backend_v2"


@pytest.mark.parametrize("alias", sorted(_DEVOPS_TEAM_ALIASES))
def test_devops_aliases_route_to_devops_when_flag_on(monkeypatch, alias) -> None:
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, "1")
    assert _team_key(alias) == "devops"


@pytest.mark.parametrize("value", ["", "0", "false", "off", "maybe"])
def test_devops_flag_falsy_values_stay_off(monkeypatch, value) -> None:
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, value)
    assert _team_key("devops") == "backend_v2"


def test_target_matches_devops_worker_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, "1")
    assert _target_matches_agent("infra", "devops") is True
    assert _target_matches_agent("devops", "backend_v2") is False


def test_ensure_target_team_stack_specs_adds_devops_stack_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, "1")
    graph = TaskGraphService(job_id="j1")
    graph.add_task("deploy", title="Deploy service", target_team="devops")

    stacks = orch_mod._ensure_target_team_stack_specs([], graph.get_tasks())

    assert dict(_DEVOPS_STACK_SPEC) in stacks
    assert not any(s.get("name") == "backend_v2" for s in stacks)


def test_ensure_target_team_stack_specs_does_not_add_devops_stack_when_flag_off(
    monkeypatch,
) -> None:
    monkeypatch.delenv(_DEVOPS_ROUTING_ENV, raising=False)
    graph = TaskGraphService(job_id="j1")
    graph.add_task("deploy", title="Deploy service", target_team="devops")

    stacks = orch_mod._ensure_target_team_stack_specs([], graph.get_tasks())

    assert dict(_BACKEND_V2_STACK_SPEC) in stacks
    assert not any(s.get("name") == "devops" for s in stacks)


def test_worker_team_key_for_devops_worker() -> None:
    """A worker with a fixed ``team_kind == "devops"`` reports the "devops"
    scheduler key directly, ungated -- such a worker can only ever be
    constructed when the routing flag is already on (see worker_factory)."""
    from types import SimpleNamespace

    worker = SimpleNamespace(team_kind="devops", stack_spec=None)
    assert orch_mod._worker_team_key(worker) == "devops"


def test_v2_team_kind_for_stack_devops(monkeypatch) -> None:
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, "1")
    assert _v2_team_kind_for_stack(StackSpec(name="devops", tools_services=[])) == "devops"
    monkeypatch.delenv(_DEVOPS_ROUTING_ENV, raising=False)
    assert _v2_team_kind_for_stack(StackSpec(name="devops", tools_services=[])) == "backend"


def test_backend_stack_with_devops_tooling_stays_backend_when_flag_on(monkeypatch) -> None:
    """A backend-named stack that merely lists IaC/CI tools among its
    tools_services must not be misrouted to a devops worker -- devops routing
    is explicit-label-only (_BACKEND_HINTS is untouched)."""
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, "1")
    spec = StackSpec(name="platform", tools_services=["Terraform", "CI/CD"])
    assert _v2_team_kind_for_stack(spec) == "backend"


def test_devops_assignment_end_to_end_flag_on(tmp_path, monkeypatch) -> None:
    """A target_team="devops" task lands on the devops worker and no other,
    once CODING_TEAM_DEVOPS_ROUTING is enabled."""
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, "1")

    class AssignDevOpsTL(StubTechLead):
        def run_assignments(self, agent_ids, ready_tasks, free_agents):
            return {"assignments": [{"agent_id": "devops_worker", "task_id": "provision"}]}

    devops_worker = StubWorker("devops_worker")
    devops_worker.team_kind = "devops"
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2"), devops_worker]
    swarm, graph = _make_swarm(tmp_path, AssignDevOpsTL(approved=True), workers)
    graph.add_task("provision", title="Provision infra", target_team="devops")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2", "devops_worker"])

    task = graph.get_task("provision")
    assert task.assigned_agent_id == "devops_worker"
    assert graph.get_task_for_agent("frontend_v2") is None
    assert graph.get_task_for_agent("backend_v2") is None


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
    """Raw worker IDs with suffixes still compare by their canonical v2 team.

    ``devops`` -> ``backend_v2_worker_1`` documents CODING_TEAM_DEVOPS_ROUTING's
    default-OFF behavior.
    """
    assert _target_matches_agent("frontend_v2", "frontend-v2-worker-2") is True
    assert _target_matches_agent("devops", "backend_v2_worker_1") is True
    assert _target_matches_agent("frontend_v2", "backend_v2_worker_1") is False


def test_team_key_warns_on_ambiguous_frontend_backend_label(caplog) -> None:
    """Ambiguous raw labels are visible in logs while preserving current precedence."""
    with caplog.at_level(logging.WARNING, logger=orch_mod.logger.name):
        assert _team_key("frontend-backend") == "frontend_v2"

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
    assert _team_key(label) == expected


def test_quality_gate_type_uses_v2_stack_inference_for_hint_stack_names() -> None:
    """Hint-only stack names still map to canonical quality gate agent types."""
    assert _quality_gate_agent_type("Angular") == "frontend"
    assert _quality_gate_agent_type("Spring Boot") == "backend"
    assert _quality_gate_agent_type("Python") == "backend"


def test_v2_team_kind_matches_frontend_hint_tokens_without_substrings() -> None:
    """Frontend hint aliases must match as tokens, not substrings inside unrelated words."""
    assert _v2_team_kind_for_stack(StackSpec(name="UI", tools_services=[])) == "frontend"
    assert _v2_team_kind_for_stack(StackSpec(name="build", tools_services=[])) == "backend"
    assert (
        _v2_team_kind_for_stack(StackSpec(name="documentation", tools_services=["guides"])) is None
    )
    assert (
        _v2_team_kind_for_stack(StackSpec(name="release automation", tools_services=["CI build"]))
        == "backend"
    )


@pytest.mark.parametrize("stack_name", ["platform", "ci_cd", "services"])
def test_v2_team_kind_accepts_backend_alias_stack_names(stack_name: str) -> None:
    """Backend-owned alias stack names build backend v2 workers instead of failing."""
    assert _v2_team_kind_for_stack(StackSpec(name=stack_name, tools_services=[])) == "backend"


@pytest.mark.parametrize("stack_name", ["default", "Senior Software Engineer"])
def test_v2_team_kind_accepts_legacy_default_stack_names(stack_name: str) -> None:
    """Legacy generic stack names now route to backend v2 after removing the Senior SWE worker."""
    assert _v2_team_kind_for_stack(StackSpec(name=stack_name, tools_services=[])) == "backend"


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

    def _build_worker(agent_id, spec, llm_getter, engine_provider, **kwargs):
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
    assert any(update.get("stack_specs") == [_BACKEND_V2_STACK_SPEC] for update in updates)


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
    result = _v2_text_mode_llm(model)

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
    result = _v2_text_mode_llm(broken)

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


def test_build_implementation_worker_returns_devops_worker_when_flag_on(monkeypatch) -> None:
    """A devops stack builds a DevOpsTeamWorker directly, bypassing CodeEngineProvider,
    with the LLM handed through unwrapped (no v2 text-mode coercion)."""
    monkeypatch.setenv(_DEVOPS_ROUTING_ENV, "1")
    from software_engineering_team.devops_team_worker import DevOpsTeamWorker

    captured_keys: List[str] = []

    def _llm_getter(key: str) -> str:
        captured_keys.append(key)
        return f"{key}-client"

    worker = orch_mod._build_implementation_worker(
        "devops_worker",
        StackSpec(name="devops", tools_services=["Terraform"]),
        _llm_getter,
        engine_provider=None,  # proves the provider is genuinely bypassed
    )

    assert isinstance(worker, DevOpsTeamWorker)
    assert worker.team_kind == "devops"
    assert captured_keys == ["devops"]
    assert worker.team_lead.llm == "devops-client"


def test_build_implementation_worker_devops_stack_is_backend_when_flag_off() -> None:
    """The same stack, with the flag off, resolves to an ordinary backend V2TeamWorker."""

    class _FakeLead:
        def __init__(self, llm):
            self.llm = llm

    class _FakeProvider:
        def build_implementation_team_lead(self, team_kind, llm):
            return _FakeLead(llm)

    worker = orch_mod._build_implementation_worker(
        "devops_worker",
        StackSpec(name="devops", tools_services=["Terraform"]),
        lambda key: f"{key}-client",
        _FakeProvider(),
    )

    assert worker.team_kind == "backend"


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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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

    class StubTL(_DefaultGroomTaskMixin):
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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

    class StubTL(_DefaultGroomTaskMixin):
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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


def test_fresh_run_preserves_falsy_task_id(tmp_path, monkeypatch):
    """A task id that is falsy but not missing (0 or "") must be kept as-is, not treated as
    absent and replaced by the task_{idx} fallback."""

    class StubTL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [
                    {"id": 0, "title": "Zero id task"},
                    {"id": "", "title": "Empty id task"},
                ],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

    captured: Dict[str, TaskGraphService] = {}

    class StubSwarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            captured["graph"] = self.graph

        def run(self, **kw):
            self.graph.mark_branch_merged("0")
            self.graph.mark_branch_merged("")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", StubTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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

    graph = captured["graph"]
    assert graph.get_task("0").title == "Zero id task"
    assert graph.get_task("").title == "Empty id task"
    assert graph.get_task("task_1") is None
    assert graph.get_task("task_2") is None


# ----------------------------------------------------- task grooming (run_groom_task wiring)


def test_task_creation_grooms_every_task_after_planning(tmp_path, monkeypatch):
    """Every planned task is groomed (via run_groom_task) strictly after planning and strictly
    before the swarm is built, and the groomed acceptance_criteria/priority land on the graph's
    tasks while the planner's own dependencies (not grooming's) are kept."""
    call_order: List[str] = []

    class GroomingTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            call_order.append("plan")
            return {
                "tasks": [
                    {"id": "t1", "title": "T1", "description": "d1", "dependencies": []},
                    {"id": "t2", "title": "T2", "description": "d2", "dependencies": ["t1"]},
                ],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

        def run_groom_task(
            self, task_id, task_title, task_description, task_dependencies, plan_context
        ):
            call_order.append(f"groom:{task_id}")
            return {
                "acceptance_criteria": [f"AC for {task_id}"],
                "out_of_scope": "",
                "description_enriched": task_description,
                "priority": "high",
                "subtasks": [],
                # Grooming's own dependency opinion must be ignored — the planner's wins.
                "task_dependencies": [],
            }

    captured: Dict[str, Any] = {}

    class StubSwarm:
        def __init__(self, *a, **k):
            captured["graph"] = k["graph"]
            call_order.append("swarm_built")

        def run(self, **kw):
            pass

    monkeypatch.setattr(orch_mod, "TechLeadAgent", GroomingTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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

    assert call_order[0] == "plan"
    assert set(call_order[1:3]) == {"groom:t1", "groom:t2"}  # parallel -> order unspecified
    assert call_order[3] == "swarm_built"  # grooming finished before the swarm was built

    graph = captured["graph"]
    assert graph.get_task("t1").acceptance_criteria == ["AC for t1"]
    assert graph.get_task("t1").priority == "high"
    assert graph.get_task("t2").acceptance_criteria == ["AC for t2"]
    assert graph.get_task("t2").dependencies == ["t1"]  # planner's deps preserved, not grooming's


def test_task_creation_grooming_failure_falls_back_for_that_task_only(
    tmp_path, monkeypatch, caplog
):
    """One task's run_groom_task raising must not abort the round (parallel_map is fast-fail by
    default) -- that task falls back to ungroomed defaults while its sibling grooms normally.

    Also asserts the grooming-failure log carries the job's bound trace id via extra=."""

    class PartiallyFailingTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [
                    {"id": "t1", "title": "T1", "description": "d1", "dependencies": []},
                    {"id": "t2", "title": "T2", "description": "d2", "dependencies": []},
                ],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

        def run_groom_task(
            self, task_id, task_title, task_description, task_dependencies, plan_context
        ):
            if task_id == "t2":
                raise RuntimeError("boom")
            return {
                "acceptance_criteria": ["AC"],
                "out_of_scope": "",
                "description_enriched": task_description,
                "priority": "high",
                "subtasks": [],
                "task_dependencies": task_dependencies,
            }

    captured: Dict[str, Any] = {}

    class StubSwarm:
        def __init__(self, *a, **k):
            captured["graph"] = k["graph"]

        def run(self, **kw):
            pass

    monkeypatch.setattr(orch_mod, "TechLeadAgent", PartiallyFailingTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", StubSwarm)

    caplog.set_level(logging.WARNING)
    with bind_trace_id("groom-failure-trace-id"):
        run_coding_team_orchestrator(
            "j1",
            tmp_path,
            CodingTeamPlanInput(repo_path=str(tmp_path)),
            update_job_fn=lambda **kw: None,
            get_job_fn=lambda jid: {},
            cache_dir=tmp_path,
            get_llm=lambda key: None,
        )

    graph = captured["graph"]
    assert graph.get_task("t1").acceptance_criteria == ["AC"]  # groomed normally
    assert graph.get_task("t2").acceptance_criteria == []  # fell back to ungroomed defaults
    assert graph.get_task("t2").description == "d2"  # ungroomed description preserved

    failure_records = [r for r in caplog.records if "Tech Lead grooming failed" in r.message]
    assert failure_records, "expected the grooming-failure log to be emitted"
    assert failure_records[-1].trace_id == "groom-failure-trace-id"


def test_groom_fanout_runs_concurrently(tmp_path, monkeypatch):
    """Per-task grooming is dispatched via shared_concurrency.parallel_map, not a sequential loop:
    a barrier that only releases when both groom calls are in flight at once must be crossed. A
    serial loop would never release the barrier and would time out instead of grooming both tasks."""
    import threading

    barrier = threading.Barrier(2, timeout=10)

    class BarrierGroomingTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [
                    {"id": "t1", "title": "T1", "description": "d1", "dependencies": []},
                    {"id": "t2", "title": "T2", "description": "d2", "dependencies": []},
                ],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

        def run_groom_task(
            self, task_id, task_title, task_description, task_dependencies, plan_context
        ):
            # Blocks until both groom calls reach here; serial execution never releases it.
            barrier.wait()
            return {
                "acceptance_criteria": [f"AC for {task_id}"],
                "out_of_scope": "",
                "description_enriched": task_description,
                "priority": "medium",
                "subtasks": [],
                "task_dependencies": task_dependencies,
            }

    captured: Dict[str, Any] = {}

    class StubSwarm:
        def __init__(self, *a, **k):
            captured["graph"] = k["graph"]

        def run(self, **kw):
            pass

    monkeypatch.setattr(orch_mod, "TechLeadAgent", BarrierGroomingTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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

    # Both grooms completed ⇒ the barrier was crossed ⇒ they genuinely ran concurrently.
    graph = captured["graph"]
    assert graph.get_task("t1").acceptance_criteria == ["AC for t1"]
    assert graph.get_task("t2").acceptance_criteria == ["AC for t2"]


def test_groomed_acceptance_criteria_reaches_code_review(tmp_path, monkeypatch):
    """A task's grooming output (acceptance_criteria), once populated on the graph by the
    orchestrator's task-creation wiring, flows through to the Tech Lead's code review call --
    CODE_REVIEW_USER surfaces acceptance_criteria as though it were always populated, so it must
    actually be populated by the time a task reaches review."""
    _patch_git(monkeypatch)

    class GroomingTL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan_input):
            return {
                "tasks": [{"id": "t1", "title": "T1", "description": "d1", "dependencies": []}],
                "stacks": [{"name": "backend", "tools_services": []}],
            }

        def run_groom_task(
            self, task_id, task_title, task_description, task_dependencies, plan_context
        ):
            return {
                "acceptance_criteria": ["Must handle empty input", "Must return 200"],
                "out_of_scope": "auth",
                "description_enriched": task_description,
                "priority": "medium",
                "subtasks": [],
                "task_dependencies": task_dependencies,
            }

    captured: Dict[str, Any] = {}

    class CapturingSwarm:
        def __init__(self, *a, **k):
            captured["graph"] = k["graph"]

        def run(self, **kw):
            pass  # only the task-creation wiring is under test here

    monkeypatch.setattr(orch_mod, "TechLeadAgent", GroomingTL)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", CapturingSwarm)

    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: None,
        get_job_fn=lambda jid: {},
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    graph = captured["graph"]
    assert graph.get_task("t1").acceptance_criteria == [
        "Must handle empty input",
        "Must return 200",
    ]

    # Drive the real review path against this groomed task and assert the acceptance criteria the
    # new wiring populated is exactly what reaches run_code_review.
    class RecordingTechLead(StubTechLead):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.acceptance_criteria_calls: List[List[str]] = []

        def run_code_review(self, task_title, task_description, acceptance_criteria, **kw):
            self.acceptance_criteria_calls.append(list(acceptance_criteria))
            return super().run_code_review(task_title, task_description, acceptance_criteria, **kw)

    tech_lead = RecordingTechLead(approved=True)
    worker = StubWorker("backend")
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=[worker],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=["backend"],
        llm_getter=lambda key: None,
    )
    graph.assign_task_to_agent("t1", "backend")
    graph.set_task_in_review("t1")

    swarm._review_and_merge(lambda **kw: None)

    assert tech_lead.acceptance_criteria_calls == [["Must handle empty input", "Must return 200"]]
    assert graph.get_task("t1").status == TaskStatus.MERGED


def test_sanitized_subtasks_drops_entries_without_id():
    """A groomed subtask missing 'id' would blow up Subtask/Task construction -- filtered out."""
    raw = [
        {"id": "s1", "title": "Sub 1"},
        {"title": "no id, dropped"},
        "not a dict",
        None,
    ]
    assert orch_mod._sanitized_subtasks(raw) == [{"id": "s1", "title": "Sub 1"}]
    assert orch_mod._sanitized_subtasks(None) == []
    assert orch_mod._sanitized_subtasks([]) == []


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
    class StubTL(_DefaultGroomTaskMixin):
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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
        def run_implement(self, task, path):
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
    assert progress_mod._no_change_revisit_cap() == progress_mod.NO_CHANGE_REVISIT_CAP
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "7")
    assert progress_mod._no_change_revisit_cap() == 7
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "garbage")
    assert progress_mod._no_change_revisit_cap() == progress_mod.NO_CHANGE_REVISIT_CAP
    monkeypatch.setenv("CODING_TEAM_NO_CHANGE_REVISIT_CAP", "0")
    assert progress_mod._no_change_revisit_cap() == 1  # floored, guard can never be disabled


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

    class StubTL(_DefaultGroomTaskMixin):
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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

    class StubTL(_DefaultGroomTaskMixin):
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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


def test_default_stack_specs_returns_independent_copy():
    """Mutating one call's result must never leak into the module template or another caller."""
    a = orch_mod._default_stack_specs()
    b = orch_mod._default_stack_specs()
    assert a == b
    assert a is not b
    a[0]["tools_services"].append("Rust")
    assert "Rust" not in orch_mod._DEFAULT_STACK_SPECS[0]["tools_services"]
    assert "Rust" not in orch_mod._default_stack_specs()[0]["tools_services"]


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
            spec_content="",
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from llm_service.interface import LLMRateLimitError
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
        assert progress_mod._coding_progress([], base, span) == base
        assert progress_mod._coding_progress(tasks(0, 0, 4), base, span) == base
        half = progress_mod._coding_progress(tasks(1, 1, 2), base, span)
        full = progress_mod._coding_progress(tasks(3, 1, 0), base, span)
        assert base <= half <= full == base + span <= 100

    # An impossible band is a caller bug, not something to render.
    with pytest.raises(AssertionError):
        progress_mod._coding_progress([], 50, 60)


def test_orchestrator_writes_job_progress_through_coding_phase(tmp_path, monkeypatch):
    """The job-level progress bar must advance during the coding phase: base at coding
    start, per-snapshot updates from the task graph, and 100 on terminal completion."""
    updates: List[Dict[str, Any]] = []

    class _PlanningTechLead(_DefaultGroomTaskMixin):
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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
    assert progresses[0] == progress_mod._DEFAULT_PROGRESS_BASE
    # 1 of 2 tasks terminal mid-run
    expected_mid = progress_mod._DEFAULT_PROGRESS_BASE + int(
        progress_mod._DEFAULT_PROGRESS_SPAN / 2
    )
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
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
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
    restored = progress_mod._DEFAULT_PROGRESS_BASE + int(progress_mod._DEFAULT_PROGRESS_SPAN / 2)
    assert progresses[0] == restored, "restored persist must reflect merged tasks"
    assert progresses == sorted(progresses), "no write may regress the bar on resume"
    assert progresses[-1] == 100


# ------------------------------------ user decisions surfaced to the review gates


def _capture_review_prompt(monkeypatch):
    """Patch the Tech Lead's LLM call to record the rendered review prompt and approve."""
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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


def test_review_prompt_includes_spec_content(monkeypatch):
    """The plan's spec content is rendered into the review prompt — this is the swarm's sole
    code-review call, so it is the only place spec constraints outside the task's own
    description/acceptance criteria can be checked."""
    tl, captured = _capture_review_prompt(monkeypatch)

    out = tl.run_code_review("t", "d", [], "evidence", spec_content="All endpoints require auth.")

    assert out["approved"] is True
    assert "Project specification" in captured["prompt"]
    assert "All endpoints require auth." in captured["prompt"]


def test_review_prompt_omits_spec_content_block_when_empty(monkeypatch):
    """No spec content → prompt is unchanged from the pre-feature behavior (no stray header)."""
    tl, captured = _capture_review_prompt(monkeypatch)

    tl.run_code_review("t", "d", [], "evidence")
    assert "Project specification" not in captured["prompt"]

    # Whitespace-only spec content is treated as absent, not rendered as an empty block.
    captured.clear()
    tl.run_code_review("t", "d", [], "evidence", spec_content="   ")
    assert "Project specification" not in captured["prompt"]


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
        spec_content="",
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
            spec_content="",
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
            spec_content="",
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
    assert progress_mod._review_concurrency() == progress_mod.REVIEW_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_REVIEW_CONCURRENCY", "not-a-number")
    assert progress_mod._review_concurrency() == progress_mod.REVIEW_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_REVIEW_CONCURRENCY", "0")
    assert progress_mod._review_concurrency() == 1  # floored so review always progresses
    monkeypatch.setenv("CODING_TEAM_REVIEW_CONCURRENCY", "7")
    assert progress_mod._review_concurrency() == 7


def test_groom_concurrency_env_parsing(monkeypatch):
    """CODING_TEAM_GROOM_CONCURRENCY: default when unset/garbage, floored at 1, honored otherwise."""
    monkeypatch.delenv("CODING_TEAM_GROOM_CONCURRENCY", raising=False)
    assert progress_mod._groom_concurrency() == progress_mod.GROOM_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_GROOM_CONCURRENCY", "not-a-number")
    assert progress_mod._groom_concurrency() == progress_mod.GROOM_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_GROOM_CONCURRENCY", "0")
    assert progress_mod._groom_concurrency() == 1  # floored so grooming always progresses
    monkeypatch.setenv("CODING_TEAM_GROOM_CONCURRENCY", "7")
    assert progress_mod._groom_concurrency() == 7


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

        def run_implement(self, task, path):
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

        def run_implement(self, task, path):
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

        def run_implement(self, task, path):
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

        def run_implement(self, task, path):
            self.calls += 1
            raise RuntimeError("implementation blew up")

    class _OkWorker:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.stack_spec = StackSpec(name=agent_id, tools_services=[])

        def run_implement(self, task, path):
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
    assert progress_mod._implementation_concurrency() == progress_mod.IMPLEMENTATION_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", "not-a-number")
    assert progress_mod._implementation_concurrency() == progress_mod.IMPLEMENTATION_CONCURRENCY
    monkeypatch.setenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", "0")
    assert (
        progress_mod._implementation_concurrency() == 1
    )  # floored so implementation always progresses
    monkeypatch.setenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", "7")
    assert progress_mod._implementation_concurrency() == 7


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


def test_review_uses_fresh_agent_per_call_not_shared(monkeypatch):
    """run_code_review must build a fresh review Agent per call (never reuse a shared instance), so
    concurrent reviews don't race on a Strands Agent's mutable conversation history."""
    from software_engineering_team.tech_lead_agent import agent as tl_mod

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
            spec_content="",
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
            spec_content="",
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
