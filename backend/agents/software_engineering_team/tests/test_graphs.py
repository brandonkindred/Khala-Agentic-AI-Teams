"""Tests for the SE team's Strands-graph builder modules.

These modules are thin wrappers around ``shared_graph.build_*`` helpers; the
tests only need to import the builders and confirm they return a usable
graph/swarm object — full graph execution is exercised by integration tests.
"""

from __future__ import annotations


def test_build_se_top_level_graph_returns_graph() -> None:
    from software_engineering_team.graphs.top_level import build_se_top_level_graph

    graph = build_se_top_level_graph()
    # Strands Graph exposes nodes attribute or similar; just confirm not None
    assert graph is not None


def test_make_se_agent_returns_strands_agent() -> None:
    from software_engineering_team.graphs.shared import make_se_agent

    agent = make_se_agent(
        name="my_agent",
        system_prompt="You are a test agent",
        description="Test desc",
    )
    assert agent is not None
    # Strands Agents expose a callable interface
    assert callable(agent)


def test_make_se_agent_default_agent_key() -> None:
    from software_engineering_team.graphs.shared import make_se_agent

    agent = make_se_agent(name="agent2", system_prompt="prompt")
    assert agent is not None


def test_build_phase2_design_graph_returns_graph() -> None:
    from software_engineering_team.devops_team.graphs.phase2_design import (
        build_phase2_design_graph,
    )

    graph = build_phase2_design_graph()
    assert graph is not None


def test_build_phase4_validation_graph_returns_graph() -> None:
    from software_engineering_team.devops_team.graphs.phase4_validation import (
        build_phase4_validation_graph,
    )

    graph = build_phase4_validation_graph()
    assert graph is not None


def test_build_review_gates_graph_returns_graph() -> None:
    from software_engineering_team.frontend_code_v2_team.graphs.review_gates import (
        build_review_gates_graph,
    )

    graph = build_review_gates_graph()
    assert graph is not None


def test_build_review_gates_graph_with_retries() -> None:
    from software_engineering_team.frontend_code_v2_team.graphs.review_gates import (
        build_review_gates_graph,
    )

    graph = build_review_gates_graph(max_fix_retries=5)
    assert graph is not None


def test_build_resolution_swarm_returns_swarm() -> None:
    from software_engineering_team.integration_team.graphs.resolution_swarm import (
        build_resolution_swarm,
    )

    swarm = build_resolution_swarm()
    assert swarm is not None


def test_build_resolution_swarm_with_handoff_limit() -> None:
    from software_engineering_team.integration_team.graphs.resolution_swarm import (
        build_resolution_swarm,
    )

    swarm = build_resolution_swarm(max_handoffs=2)
    assert swarm is not None


def test_review_result_protocol_runtime_checkable() -> None:
    """The ``ReviewResult`` Protocol should runtime-check ``approved: bool``."""
    from software_engineering_team.quality_gates.protocols import ReviewResult

    class _Approved:
        approved: bool = True

    class _NoAttr:
        pass

    assert isinstance(_Approved(), ReviewResult)
    assert not isinstance(_NoAttr(), ReviewResult)
