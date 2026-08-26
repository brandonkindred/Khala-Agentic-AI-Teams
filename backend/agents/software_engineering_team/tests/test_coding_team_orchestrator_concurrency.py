"""Orchestrator-level concurrency proof for a roster widened via
``CODING_TEAM_WORKERS_PER_STACK``.

``test_coding_team_agent_statuses.py`` covers ``derive_stack_roster``'s id generation in
isolation, and ``test_coding_team_worktree_manager.py`` covers per-agent worktree isolation
under concurrent threads. Neither exercises the actual orchestrator entry point
(``run_coding_team_orchestrator``) with a real plan, so neither proves that 2+ same-stack
tasks are genuinely scheduled concurrently — as opposed to merely being *assignable* to
distinct agents. This module closes that gap: it drives ``run_coding_team_orchestrator``
for real (only the Tech Lead, implementation-worker construction, and worktree manager are
stubbed — the same boundaries every other orchestrator test stubs) with a single backend_v2
stack widened to 2 workers, and proves the two resulting tasks execute concurrently via an
explicit ``threading.Barrier`` rather than a wall-clock/sleep timing assertion, so the test
stays deterministic under CI.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from software_engineering_team import coding_team_orchestrator as orch_mod
from software_engineering_team.coding_team_orchestrator import (
    CodingTeamSwarm,
    run_coding_team_orchestrator,
)
from software_engineering_team.models import CodingTeamPlanInput
from software_engineering_team.team_routing import _BACKEND_V2_STACK_SPEC

from ._coding_team_orchestrator_doubles import DefaultGroomTaskMixin as _DefaultGroomTaskMixin
from ._coding_team_orchestrator_doubles import FakeWorktreeManager as _FakeWorktreeManager
from ._coding_team_orchestrator_doubles import patch_git as _patch_git


class _TwoBackendTasksTechLead(_DefaultGroomTaskMixin):
    """Plans exactly two backend_v2-targeted tasks and approves every review unconditionally.

    Both tasks share the identical ``target_team`` and resolve to the identical free-agent
    pool, so ``_try_homogeneous_target_assign`` (swarm_assignment.py) places them without any
    LLM call — ``run_assignments`` is deliberately not implemented here, since it must never
    be reached for this scenario.
    """

    def __init__(self, llm):
        pass

    def run_plan_to_task_graph(self, plan_input):
        return {
            "tasks": [
                {"id": "t1", "title": "T1", "target_team": "backend_v2"},
                {"id": "t2", "title": "T2", "target_team": "backend_v2"},
            ],
            "stacks": [dict(_BACKEND_V2_STACK_SPEC)],
        }

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
        return {"approved": True, "reason": "", "requested_changes": []}


def _run_two_backend_tasks(
    tmp_path,
    monkeypatch,
    run_implement: Callable[[str, Any, Path], Dict[str, Any]],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Runs ``run_coding_team_orchestrator`` for real with CODING_TEAM_WORKERS_PER_STACK=2
    and 2 backend_v2-targeted tasks. ``run_implement(agent_id, task, path)`` is called by
    each built worker in place of a real implementation.

    Returns (built_agent_ids, status_updates): the agent ids ``_build_implementation_worker``
    was asked to build (in build order — this is ``derive_stack_roster``'s real widened
    output, unmocked), and every ``update_job_fn`` call the run made, in order.
    """
    monkeypatch.setenv("CODING_TEAM_WORKERS_PER_STACK", "2")
    # Pin explicitly rather than relying on the default: an ambient
    # CODING_TEAM_IMPLEMENTATION_CONCURRENCY=1 in the environment would otherwise make this
    # test fail for a reason that has nothing to do with the orchestrator's own scheduling.
    monkeypatch.setenv("CODING_TEAM_IMPLEMENTATION_CONCURRENCY", "2")
    _patch_git(monkeypatch)
    monkeypatch.setattr(orch_mod, "TechLeadAgent", _TwoBackendTasksTechLead)
    monkeypatch.setattr(orch_mod, "WorktreeManager", _FakeWorktreeManager)
    # Bypass the external quality-gate tools (build/lint/code-review) — not under test here,
    # same bypass _make_swarm applies in test_coding_team_orchestrator.py.
    monkeypatch.setattr(CodingTeamSwarm, "_run_quality_gates", lambda self, *a, **k: True)

    built_agent_ids: List[str] = []

    def _build_worker(agent_id, spec, llm_getter, engine_provider, review_context=None):
        built_agent_ids.append(agent_id)

        class _ScriptedWorker:
            def __init__(self) -> None:
                self.agent_id = agent_id
                self.team_kind = "backend"
                self.stack_spec = spec

            def run_implement(self, task, path):
                return run_implement(agent_id, task, path)

        return _ScriptedWorker()

    monkeypatch.setattr(orch_mod, "_build_implementation_worker", _build_worker)

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
    return built_agent_ids, updates


def test_two_same_stack_tasks_execute_concurrently_and_merge_correctly(tmp_path, monkeypatch):
    """CODING_TEAM_WORKERS_PER_STACK=2 widens the single backend_v2 stack into 2 agent ids
    (backend_v2-1, backend_v2-2) via the real, unmocked ``derive_stack_roster``. Both
    backend-only tasks must be assigned to and implemented by the 2 distinct agents in the
    same round.

    A 2-party ``threading.Barrier`` proves genuine concurrency deterministically: it only
    releases once both workers are inside ``run_implement`` at the same time. A regression
    that (re-)serializes same-stack execution — the exact failure this roster-widening
    feature exists to fix — would time out the barrier, and the run would end with at least
    one task bounced for revision instead of both reaching MERGED.

    Recording each call's (agent_id, task_id) pair with no lock around the append also
    guards against corruption of the Task Graph's one-active-task-per-agent invariant under
    concurrent scheduling: a bug that let the same task run twice, or the same agent run
    twice, or dropped a task, would show up directly in the recorded pairs.
    """
    barrier = threading.Barrier(2, timeout=10)
    calls: List[Tuple[str, str]] = []

    def run_implement(agent_id, task, path):
        # Blocks until both workers reach here; serial (or re-serialized) execution never
        # releases it, and the test fails on a BrokenBarrierError instead of hanging.
        barrier.wait()
        calls.append((agent_id, task.id))
        return {
            "status": "in_review",
            "feature_branch": f"feature/{task.id}",
            "changes_summary": f"did {task.id} on {agent_id}",
            "files_to_create_or_edit": [],
            "error": None,
        }

    built_agent_ids, updates = _run_two_backend_tasks(tmp_path, monkeypatch, run_implement)

    assert set(built_agent_ids) == {"backend_v2-1", "backend_v2-2"}

    # Both tasks ran, each on a distinct agent, none dropped or double-run.
    assert len(calls) == 2
    assert {agent_id for agent_id, _ in calls} == {"backend_v2-1", "backend_v2-2"}
    assert {task_id for _, task_id in calls} == {"t1", "t2"}

    assert updates, "orchestrator must write at least the terminal status"
    final = updates[-1]
    assert final["status"] == "completed"
    assert "2 merged, 0 failed" in final["status_text"]
