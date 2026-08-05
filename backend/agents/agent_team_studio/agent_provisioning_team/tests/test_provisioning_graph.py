"""Unit test for the provisioning graph builder (`graphs/provisioning_graph.py`)."""

from __future__ import annotations


def test_build_provisioning_graph_wires_six_sequential_phases() -> None:
    from strands.multiagent.graph import Graph

    from agent_team_studio.agent_provisioning_team.graphs.provisioning_graph import (
        build_provisioning_graph,
    )

    g = build_provisioning_graph()
    assert isinstance(g, Graph)
    # The six provisioning phases, each a distinct node.
    assert set(g.nodes) == {
        "setup",
        "credential_generation",
        "account_provisioning",
        "access_audit",
        "documentation",
        "deliver",
    }
    # Sequential wiring: N nodes chained end to end → N-1 edges, one entry point.
    assert len(g.edges) == len(g.nodes) - 1
    assert len(g.entry_points) == 1
