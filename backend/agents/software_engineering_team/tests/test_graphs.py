"""Tests for the SE team's Strands-graph builder modules.

These modules are thin wrappers around ``shared.graph.build_*`` helpers; the
tests only need to import the builders and confirm they return a usable
graph/swarm object — full graph execution is exercised by integration tests.
"""

from __future__ import annotations


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
