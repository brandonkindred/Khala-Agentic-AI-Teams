"""Reusable Graph construction patterns.

Codifies common topologies observed across teams:
- Fan-out/fan-in (parallel agents -> single compositor)
- Sequential pipeline (chain of agents)
"""

from __future__ import annotations

from typing import Union

from strands import Agent
from strands.multiagent.graph import Graph, GraphBuilder, GraphNode
from strands.multiagent.swarm import Swarm

NodeType = Union[Agent, Graph, Swarm]


def wire_fan_out_fan_in(
    builder: GraphBuilder,
    agents: list[tuple[str, NodeType]],
    fan_in_node: GraphNode,
) -> None:
    """Wire a fan-out/fan-in topology onto an already-open *builder*.

    Lower-level counterpart to :func:`build_fan_out_fan_in` for callers that must
    keep composing *builder* afterward (e.g. adding more nodes/edges downstream of
    *fan_in_node*) instead of receiving a finished ``Graph``.

    Preconditions:
        *agents* is non-empty. *fan_in_node* is the ``GraphNode`` already returned
        by ``builder.add_node(...)`` on the same *builder*.
    Postconditions:
        Every ``(node_id, agent)`` pair is added as a node on *builder*, edged to
        *fan_in_node*, and registered as an entry point.
    """
    assert agents, "agents must be non-empty"
    for node_id, agent in agents:
        node = builder.add_node(agent, node_id=node_id)
        builder.set_entry_point(node_id)
        builder.add_edge(node, fan_in_node)


def build_fan_out_fan_in(
    *,
    agents: list[tuple[str, NodeType]],
    compositor: tuple[str, NodeType],
    graph_id: str = "fan_out_fan_in",
    execution_timeout: float = 600.0,
    node_timeout: float = 180.0,
) -> Graph:
    """Build a fan-out/fan-in graph: N parallel agents feeding one compositor.

    Parameters
    ----------
    agents:
        List of ``(node_id, agent_or_subgraph)`` tuples for parallel fan-out.
    compositor:
        ``(node_id, agent_or_subgraph)`` that merges all fan-out outputs.
    graph_id:
        Identifier for the graph.
    execution_timeout:
        Overall graph timeout in seconds.
    node_timeout:
        Per-node timeout in seconds.

    Preconditions:
        *agents* is non-empty (enforced by the delegated
        :func:`wire_fan_out_fan_in`, which raises ``AssertionError`` otherwise).

    Returns
    -------
    Graph
        A Strands ``Graph`` ready for invocation.
    """
    builder = GraphBuilder()
    builder.set_graph_id(graph_id)
    builder.set_execution_timeout(execution_timeout)
    builder.set_node_timeout(node_timeout)

    comp_node = builder.add_node(compositor[1], node_id=compositor[0])
    wire_fan_out_fan_in(builder, agents, comp_node)

    return builder.build()


def build_sequential(
    *,
    stages: list[tuple[str, NodeType]],
    graph_id: str = "sequential_pipeline",
    execution_timeout: float = 600.0,
    node_timeout: float = 180.0,
) -> Graph:
    """Build a sequential pipeline graph: stage1 -> stage2 -> ... -> stageN.

    Parameters
    ----------
    stages:
        Ordered list of ``(node_id, agent_or_subgraph)`` tuples.
    graph_id:
        Identifier for the graph.
    execution_timeout:
        Overall graph timeout in seconds.
    node_timeout:
        Per-node timeout in seconds.

    Returns
    -------
    Graph
        A Strands ``Graph`` ready for invocation.
    """
    if not stages:
        raise ValueError("stages must contain at least one entry")

    builder = GraphBuilder()
    builder.set_graph_id(graph_id)
    builder.set_execution_timeout(execution_timeout)
    builder.set_node_timeout(node_timeout)

    prev_node = None
    for i, (node_id, agent) in enumerate(stages):
        node = builder.add_node(agent, node_id=node_id)
        if i == 0:
            builder.set_entry_point(node_id)
        if prev_node is not None:
            builder.add_edge(prev_node, node)
        prev_node = node

    return builder.build()
