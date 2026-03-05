"""Tests for PlanningGraph and compiler."""

from __future__ import annotations

import pytest

from planning_team.planning_graph import (
    PlanningDomain,
    PlanningEdge,
    PlanningGraph,
    PlanningNode,
    PlanningNodeKind,
    EdgeType,
    Phase,
    ensure_dict,
    ensure_str_list,
)


def test_planning_graph_add_and_merge():
    """PlanningGraph supports add_node, add_edge, and merge."""
    g = PlanningGraph()
    n1 = PlanningNode(id="backend-api", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.STORY, summary="API")
    g.add_node(n1)
    g.add_edge(PlanningEdge(from_id="git-setup", to_id="backend-api", type=EdgeType.BLOCKS))

    assert "backend-api" in g.nodes
    assert len(g.edges) == 1

    g2 = PlanningGraph()
    n2 = PlanningNode(id="frontend-ui", domain=PlanningDomain.FRONTEND, kind=PlanningNodeKind.STORY, summary="UI")
    g2.add_node(n2)
    g.merge(g2)
    assert "frontend-ui" in g.nodes
    assert len(g.nodes) == 2


def test_ensure_str_list_handles_malformed_inputs():
    """ensure_str_list coerces None, string, and non-list values to List[str]."""
    assert ensure_str_list(None) == []
    assert ensure_str_list("") == []
    assert ensure_str_list("single") == ["single"]
    assert ensure_str_list(["a", "b"]) == ["a", "b"]
    assert ensure_str_list([]) == []
    assert ensure_str_list(123) == []


def test_ensure_dict_handles_malformed_inputs():
    """ensure_dict coerces None and non-dict values to Dict."""
    assert ensure_dict(None) == {}
    assert ensure_dict({}) == {}
    assert ensure_dict({"a": 1}) == {"a": 1}
    assert ensure_dict("invalid") == {}


def test_planning_node_from_malformed_llm_dict():
    """PlanningNode can be built from LLM dict with inputs/outputs/acceptance_criteria as string or null."""
    n = {
        "id": "frontend-task-list",
        "inputs": "api",  # LLM sometimes returns string instead of list
        "outputs": None,
        "acceptance_criteria": "one item",  # string instead of list
        "metadata": "invalid",  # non-dict
    }
    node = PlanningNode(
        id=n["id"],
        domain=PlanningDomain.FRONTEND,
        kind=PlanningNodeKind.STORY,
        summary=n.get("summary", ""),
        details=n.get("details", ""),
        inputs=ensure_str_list(n.get("inputs")),
        outputs=ensure_str_list(n.get("outputs")),
        acceptance_criteria=ensure_str_list(n.get("acceptance_criteria")),
        metadata=ensure_dict(n.get("metadata")),
    )
    assert node.id == "frontend-task-list"
    assert node.inputs == ["api"]
    assert node.outputs == []
    assert node.acceptance_criteria == ["one item"]
    assert node.metadata == {}
