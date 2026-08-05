"""Tests for the shared pure step-ordering used by the runner and the Temporal workflow."""

from __future__ import annotations

from agent_team_studio.agentic_team_provisioning.step_ordering import order_step_ids


def test_empty():
    assert order_step_ids([]) == []


def test_linear_chain_from_entry_point():
    steps = [("b", []), ("a", ["b"]), ("c", ["a"])]  # entry is c (referenced by none)
    assert order_step_ids(steps) == ["c", "a", "b"]


def test_branching_breadth_first():
    steps = [
        ("root", ["l", "r"]),
        ("l", ["leaf"]),
        ("r", ["leaf"]),
        ("leaf", []),
    ]
    assert order_step_ids(steps) == ["root", "l", "r", "leaf"]


def test_cycle_with_no_entry_uses_first_step():
    steps = [("a", ["b"]), ("b", ["a"])]  # both referenced -> no entry point
    assert order_step_ids(steps) == ["a", "b"]


def test_unreachable_steps_appended_in_input_order():
    steps = [("a", []), ("orphan2", []), ("orphan1", [])]
    # All three are entry points (none referenced); preserved in input order.
    assert order_step_ids(steps) == ["a", "orphan2", "orphan1"]


def test_dangling_next_edge_ignored():
    steps = [("a", ["missing"])]  # 'missing' is not a real step
    assert order_step_ids(steps) == ["a"]


def test_matches_runner_topological_sort():
    """The runner's ProcessStep-based sort must produce the same order as the shared
    primitive, since it now delegates to it."""
    from agent_team_studio.agentic_team_provisioning.models import ProcessStep, StepType
    from agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner import PipelineRunner

    steps = [
        ProcessStep(step_id="b", name="B", step_type=StepType.ACTION, next_steps=["c"]),
        ProcessStep(step_id="a", name="A", step_type=StepType.ACTION, next_steps=["b"]),
        ProcessStep(step_id="c", name="C", step_type=StepType.ACTION, next_steps=[]),
    ]
    ordered = PipelineRunner._topological_sort(steps)
    assert [s.step_id for s in ordered] == ["a", "b", "c"]
