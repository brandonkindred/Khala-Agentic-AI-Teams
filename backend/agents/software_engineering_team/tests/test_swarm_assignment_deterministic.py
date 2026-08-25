"""Tests for the deterministic assignment fast-path in ``_AssignmentMixin._assign_tasks``.

Proves the pure-Python fast path (``_try_deterministic_assign``, added alongside
``_reserve_pinned_tasks``) skips ``tech_lead.run_assignments`` when the ready-task-to-
free-agent mapping is unambiguous, and that assignment still falls through to the
unchanged Tech-Lead LLM path whenever any ambiguity remains.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from software_engineering_team.coding_team_orchestrator import CodingTeamSwarm
from software_engineering_team.models import StackSpec
from software_engineering_team.task_graph import TaskGraphService


class _RecordingTechLead:
    """Duck-typed Tech Lead whose ``run_assignments`` records every call it receives.

    Assignment proposals are the naive "zip ready tasks to free agents in order" shape —
    good enough to exercise the LLM-path guardrails without needing real assignment logic,
    since these tests care about *whether* the LLM was called, not its output quality.
    """

    def __init__(self) -> None:
        self.assignment_calls: List[Dict[str, Any]] = []

    def run_assignments(self, agent_ids, ready_tasks, free_agents):
        self.assignment_calls.append(
            {
                "agent_ids": list(agent_ids),
                "ready_tasks": list(ready_tasks),
                "free_agents": list(free_agents),
            }
        )
        assignments = [
            {"agent_id": a, "task_id": t["id"]} for t, a in zip(ready_tasks, free_agents)
        ]
        return {"assignments": assignments}


class StubWorker:
    """Minimal duck-typed implementation worker; ``_assign_tasks`` never calls run_implement."""

    def __init__(self, agent_id: str, stack_name: str | None = None) -> None:
        self.agent_id = agent_id
        self.stack_spec = StackSpec(name=stack_name or agent_id, tools_services=[])


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
    return swarm, graph


# --------------------------------------------------------------------------- deterministic fires


def test_disjoint_target_teams_skip_llm(tmp_path):
    """2 workers with disjoint stacks, tasks with matching target_team -> no LLM call."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("ui", title="Build UI", target_team="frontend_v2")
    graph.add_task("api", title="Build API", target_team="backend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    assert tech_lead.assignment_calls == []
    assert graph.get_task("ui").assigned_agent_id == "frontend_v2"
    assert graph.get_task("api").assigned_agent_id == "backend_v2"


def test_single_free_agent_single_ready_task_skips_llm(tmp_path):
    """1 free agent, 1 ready task (no target_team) -> the trivial case skips the LLM."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("a1")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="T1")

    swarm._assign_tasks(graph.get_tasks(), ["a1"])

    assert tech_lead.assignment_calls == []
    assert graph.get_task("t1").assigned_agent_id == "a1"


def test_pinned_task_is_reserved_before_the_deterministic_check(tmp_path):
    """A pinned task is claimed by ``_reserve_pinned_tasks`` first; the remaining unambiguous
    task then still resolves deterministically -- proving pin handling precedes, and composes
    with, the deterministic fast path rather than forcing an LLM call."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("pinned", title="Pinned")
    graph.update_task(
        "pinned", feature_branch="feature/pinned", feature_branch_agent_id="backend_v2"
    )
    graph.add_task("ui", title="Build UI", target_team="frontend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    assert tech_lead.assignment_calls == []
    assert graph.get_task("pinned").assigned_agent_id == "backend_v2"
    assert graph.get_task("ui").assigned_agent_id == "frontend_v2"


# ------------------------------------------------------------------------- ambiguity falls through


def test_untargeted_task_with_multiple_candidates_calls_llm(tmp_path):
    """2 workers, a single task with no target_team -> both agents are candidates, so the
    mapping is ambiguous and the Tech-Lead LLM path runs."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="Untargeted task")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    assert len(tech_lead.assignment_calls) == 1
    call = tech_lead.assignment_calls[0]
    assert {t["id"] for t in call["ready_tasks"]} == {"t1"}
    assert set(call["free_agents"]) == {"frontend_v2", "backend_v2"}


def test_two_tasks_targeting_the_same_team_calls_llm(tmp_path):
    """2 workers, 2 tasks both targeting the same team -> both tasks resolve to the same
    single candidate agent, which is itself an ambiguity (two tasks competing for one agent),
    so the Tech-Lead LLM path runs."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="First", target_team="frontend_v2")
    graph.add_task("t2", title="Second", target_team="frontend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    assert len(tech_lead.assignment_calls) == 1
    call = tech_lead.assignment_calls[0]
    assert {t["id"] for t in call["ready_tasks"]} == {"t1", "t2"}


def test_mixed_deterministic_and_ambiguous_tasks_calls_llm_for_the_whole_batch(tmp_path):
    """A batch mixing an unambiguous task with ambiguous ones is not partially resolved by the
    fast path -- any remaining ambiguity sends the ENTIRE remaining batch to the Tech-Lead LLM,
    including the task that would otherwise have matched deterministically."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("clear", title="Unambiguous", target_team="backend_v2")
    graph.add_task("ambig1", title="Ambiguous 1")
    graph.add_task("ambig2", title="Ambiguous 2")

    swarm._assign_tasks(graph.get_tasks(), ["frontend_v2", "backend_v2"])

    assert len(tech_lead.assignment_calls) == 1
    call = tech_lead.assignment_calls[0]
    assert {t["id"] for t in call["ready_tasks"]} == {"clear", "ambig1", "ambig2"}


def test_pinned_only_ready_pool_still_calls_llm(tmp_path):
    """When every ready task is claimed by pinned reservation, nothing remains for the
    deterministic pool (``remaining_ready`` is empty) -- ``_try_deterministic_assign`` returns
    False in that case too, so the (now-empty) round still falls through to the LLM path,
    covering the "no remaining task" branch distinct from ambiguity."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("pinned", title="Pinned")
    graph.update_task(
        "pinned", feature_branch="feature/pinned", feature_branch_agent_id="backend_v2"
    )

    swarm._assign_tasks(graph.get_tasks(), ["backend_v2"])

    assert len(tech_lead.assignment_calls) == 1
    assert graph.get_task("pinned").assigned_agent_id == "backend_v2"


# --------------------------------------------------------------------- direct unit-level coverage


def test_try_deterministic_assign_returns_true_and_assigns_on_unambiguous_mapping(tmp_path):
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("ui", title="Build UI", target_team="frontend_v2")
    ready = [graph.get_task("ui")]

    used_agents: set[str] = set()
    assigned_tasks: set[str] = set()
    result = swarm._try_deterministic_assign(
        ready, ["frontend_v2", "backend_v2"], used_agents, assigned_tasks
    )

    assert result is True
    assert used_agents == {"frontend_v2"}
    assert assigned_tasks == {"ui"}
    assert tech_lead.assignment_calls == []


def test_try_deterministic_assign_returns_false_on_duplicate_candidate(tmp_path):
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("frontend_v2"), StubWorker("backend_v2")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="First", target_team="frontend_v2")
    graph.add_task("t2", title="Second", target_team="frontend_v2")
    ready = [graph.get_task("t1"), graph.get_task("t2")]

    used_agents: set[str] = set()
    assigned_tasks: set[str] = set()
    result = swarm._try_deterministic_assign(
        ready, ["frontend_v2", "backend_v2"], used_agents, assigned_tasks
    )

    assert result is False
    assert used_agents == set()
    assert assigned_tasks == set()


def test_multiple_same_stack_free_agents_all_reported_free(tmp_path):
    """A widened roster with 2 free same-stack agents and 1 busy same-stack agent ->
    ``_find_free_agents`` reports both free agents, not just the first-listed one."""
    tech_lead = _RecordingTechLead()
    workers = [
        StubWorker("backend_v2-1", stack_name="backend_v2"),
        StubWorker("backend_v2-2", stack_name="backend_v2"),
        StubWorker("backend_v2-3", stack_name="backend_v2"),
    ]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("busy", title="Busy")
    graph.assign_task_to_agent("busy", "backend_v2-1")

    assert swarm._find_free_agents() == ["backend_v2-2", "backend_v2-3"]


def test_homogeneous_targeted_tasks_fan_out_without_llm(tmp_path):
    """2 ready tasks targeting the same stack, 2 free same-stack agents -> both are
    assigned one-per-agent without an LLM call, proving the second-listed agent is a
    real candidate too, not just the first-listed one."""
    tech_lead = _RecordingTechLead()
    workers = [
        StubWorker("backend_v2-1", stack_name="backend_v2"),
        StubWorker("backend_v2-2", stack_name="backend_v2"),
    ]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="First", target_team="backend_v2")
    graph.add_task("t2", title="Second", target_team="backend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["backend_v2-1", "backend_v2-2"])

    assert tech_lead.assignment_calls == []
    assert graph.get_task("t1").assigned_agent_id == "backend_v2-1"
    assert graph.get_task("t2").assigned_agent_id == "backend_v2-2"


def test_homogeneous_targeted_tasks_with_contention_still_calls_llm(tmp_path):
    """3 tasks target the same stack but only 1 matching free agent exists -> genuine
    contention (which task should win the scarce worker) still needs the Tech Lead."""
    tech_lead = _RecordingTechLead()
    workers = [
        StubWorker("backend_v2-1", stack_name="backend_v2"),
        StubWorker("frontend_v2-1", stack_name="frontend_v2"),
    ]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="First", target_team="backend_v2")
    graph.add_task("t2", title="Second", target_team="backend_v2")
    graph.add_task("t3", title="Third", target_team="backend_v2")

    swarm._assign_tasks(graph.get_tasks(), ["backend_v2-1", "frontend_v2-1"])

    assert len(tech_lead.assignment_calls) == 1
    call = tech_lead.assignment_calls[0]
    assert {t["id"] for t in call["ready_tasks"]} == {"t1", "t2", "t3"}


def test_homogeneous_single_task_batches_rotate_across_free_agents(tmp_path):
    """Sequential single-task batches, with both same-stack agents free every round (the
    prior task always merges before the next one arrives), rotate which agent starts the
    pairing instead of always preferring the first-listed one -- proving a stack with 2+
    workers isn't permanently reduced to using only the first whenever demand never
    actually reaches contention."""
    tech_lead = _RecordingTechLead()
    workers = [
        StubWorker("backend_v2-1", stack_name="backend_v2"),
        StubWorker("backend_v2-2", stack_name="backend_v2"),
    ]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)

    assigned_agents = []
    for i in range(4):
        task_id = f"t{i}"
        graph.add_task(task_id, title=f"Task {i}", target_team="backend_v2")
        swarm._assign_tasks([graph.get_task(task_id)], ["backend_v2-1", "backend_v2-2"])
        assigned_agents.append(graph.get_task(task_id).assigned_agent_id)
        graph.mark_branch_merged(task_id)

    assert tech_lead.assignment_calls == []
    assert set(assigned_agents) == {"backend_v2-1", "backend_v2-2"}
    assert assigned_agents == [
        "backend_v2-1",
        "backend_v2-2",
        "backend_v2-1",
        "backend_v2-2",
    ]


def test_same_stack_workers_both_do_work_across_rounds_no_starvation(tmp_path):
    """2 same-stack tasks, 2 same-stack workers fan out fairly in round 1 (both used, no
    LLM call). A later-arriving third same-stack task then sits TO_DO while both workers
    are busy, and is placed on whichever worker frees up *first* -- here backend_v2-2, the
    second-listed one -- proving assignment isn't hard-coded to always prefer
    backend_v2-1 and that no worker is starved across rounds."""
    tech_lead = _RecordingTechLead()
    workers = [
        StubWorker("backend_v2-1", stack_name="backend_v2"),
        StubWorker("backend_v2-2", stack_name="backend_v2"),
    ]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="First", target_team="backend_v2")
    graph.add_task("t2", title="Second", target_team="backend_v2")

    swarm._assign_tasks(graph.get_tasks(), swarm._find_free_agents())
    assert graph.get_task("t1").assigned_agent_id == "backend_v2-1"
    assert graph.get_task("t2").assigned_agent_id == "backend_v2-2"

    # A third same-stack task arrives once both workers are already busy.
    graph.add_task("t3", title="Third", target_team="backend_v2")
    swarm._assign_tasks(swarm._find_ready_tasks(), swarm._find_free_agents())
    assert graph.get_task("t3").status.value == "to_do"

    # backend_v2-2's task merges first this round -- the freed-up (second-listed) agent
    # gets the next task.
    graph.mark_branch_merged("t2")
    swarm._assign_tasks(swarm._find_ready_tasks(), swarm._find_free_agents())

    assert graph.get_task("t3").assigned_agent_id == "backend_v2-2"
    assert tech_lead.assignment_calls == []


def test_try_deterministic_assign_returns_false_when_nothing_remains(tmp_path):
    """Empty remaining-ready and empty remaining-free pools are both covered: an already fully
    assigned task list, and a free-agent list already exhausted by ``used_agents``."""
    tech_lead = _RecordingTechLead()
    workers = [StubWorker("a1")]
    swarm, graph = _make_swarm(tmp_path, tech_lead, workers)
    graph.add_task("t1", title="T1")
    ready = [graph.get_task("t1")]

    # remaining_ready empty: the only ready task is already marked assigned.
    assert swarm._try_deterministic_assign(ready, ["a1"], set(), {"t1"}) is False

    # remaining_free empty: the only free agent is already marked used.
    assert swarm._try_deterministic_assign(ready, ["a1"], {"a1"}, set()) is False
