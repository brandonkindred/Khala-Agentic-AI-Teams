"""Construction tests for ``shared.graph.patterns`` (fan-out/fan-in factories).

Run under ``LLM_PROVIDER=dummy`` (see ``conftest.py``): agent construction resolves
a dummy Strands model and never invokes it, so these build real ``Agent``/
``GraphBuilder`` instances with no mocking, mirroring
``branding_team/tests/test_graphs.py``'s graph-topology coverage.
"""

from __future__ import annotations

import pytest
from strands.multiagent.graph import Graph, GraphBuilder

from shared.graph import build_agent, build_fan_out_fan_in, wire_fan_out_fan_in


def _agent(name: str):
    return build_agent(name=name, system_prompt=f"do {name}")


def test_build_fan_out_fan_in_returns_graph() -> None:
    graph = build_fan_out_fan_in(
        agents=[("a", _agent("a")), ("b", _agent("b"))],
        compositor=("comp", _agent("comp")),
    )
    assert isinstance(graph, Graph)


def test_build_fan_out_fan_in_wires_entry_points_and_edges() -> None:
    graph = build_fan_out_fan_in(
        agents=[("a", _agent("a")), ("b", _agent("b")), ("c", _agent("c"))],
        compositor=("comp", _agent("comp")),
    )
    edges = {(e.from_node.node_id, e.to_node.node_id) for e in graph.edges}
    entry_point_ids = {n.node_id for n in graph.entry_points}

    assert entry_point_ids == {"a", "b", "c"}
    assert edges == {("a", "comp"), ("b", "comp"), ("c", "comp")}
    assert set(graph.nodes.keys()) == {"a", "b", "c", "comp"}
    assert "comp" not in entry_point_ids


def test_build_fan_out_fan_in_accepts_custom_graph_id_and_timeouts() -> None:
    graph = build_fan_out_fan_in(
        agents=[("a", _agent("a"))],
        compositor=("comp", _agent("comp")),
        graph_id="custom_graph",
        execution_timeout=30.0,
        node_timeout=10.0,
    )
    assert isinstance(graph, Graph)
    assert graph.id == "custom_graph"
    assert graph.execution_timeout == 30.0
    assert graph.node_timeout == 10.0


def test_build_fan_out_fan_in_defaults_graph_id_and_timeouts() -> None:
    graph = build_fan_out_fan_in(
        agents=[("a", _agent("a"))],
        compositor=("comp", _agent("comp")),
    )
    assert graph.id == "fan_out_fan_in"
    assert graph.execution_timeout == 600.0
    assert graph.node_timeout == 180.0


def test_wire_fan_out_fan_in_wires_onto_open_builder() -> None:
    """Mirrors how ``phase3_visual.py`` keeps building after the fan-in."""
    builder = GraphBuilder()
    fan_in_node = builder.add_node(_agent("collector"), node_id="collector")

    wire_fan_out_fan_in(
        builder,
        [("a", _agent("a")), ("b", _agent("b"))],
        fan_in_node,
    )

    # Caller keeps composing the same builder after the fan-in wiring.
    extra_node = builder.add_node(_agent("downstream"), node_id="downstream")
    builder.add_edge(fan_in_node, extra_node)
    graph = builder.build()

    edges = {(e.from_node.node_id, e.to_node.node_id) for e in graph.edges}
    entry_point_ids = {n.node_id for n in graph.entry_points}

    assert entry_point_ids == {"a", "b"}
    assert edges == {("a", "collector"), ("b", "collector"), ("collector", "downstream")}
    assert set(graph.nodes.keys()) == {"a", "b", "collector", "downstream"}


def test_wire_fan_out_fan_in_rejects_empty_agents() -> None:
    builder = GraphBuilder()
    fan_in_node = builder.add_node(_agent("collector"), node_id="collector")

    with pytest.raises(AssertionError, match="agents must be non-empty"):
        wire_fan_out_fan_in(builder, [], fan_in_node)
