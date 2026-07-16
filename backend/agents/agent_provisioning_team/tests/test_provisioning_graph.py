"""Unit test for the provisioning graph builder (`graphs/provisioning_graph.py`)."""

from __future__ import annotations


def test_build_provisioning_graph_returns_graph() -> None:
    from agent_provisioning_team.graphs.provisioning_graph import build_provisioning_graph

    g = build_provisioning_graph()
    assert g is not None
