"""Tests for PlanningGraph validation."""

from __future__ import annotations

from planning_team.planning_graph import (
    EdgeType,
    PlanningDomain,
    PlanningEdge,
    PlanningGraph,
    PlanningNode,
    PlanningNodeKind,
)
from planning_team.validation import format_validation_report, validate_planning_graph


def test_validate_empty_graph_passes():
    """Empty graph passes structural validation (no cycles or dangling edges)."""
    g = PlanningGraph()
    is_valid, errors = validate_planning_graph(g)
    assert is_valid
    assert len(errors) == 0


def test_validate_sufficient_tasks_passes():
    """Graph with enough backend/frontend tasks passes."""
    g = PlanningGraph()
    for i, domain in enumerate(
        [
            PlanningDomain.BACKEND,
            PlanningDomain.BACKEND,
            PlanningDomain.FRONTEND,
            PlanningDomain.FRONTEND,
        ]
    ):
        g.add_node(
            PlanningNode(
                id=f"task-{i}",
                domain=domain,
                kind=PlanningNodeKind.STORY,
                summary=f"Task {i}",
                details="Details here.",
            )
        )
    is_valid, errors = validate_planning_graph(g)
    assert is_valid
    assert len(errors) == 0


def test_validate_detects_cycle():
    """Validation detects BLOCKS cycle."""
    g = PlanningGraph()
    g.add_node(
        PlanningNode(
            id="a", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.STORY, summary="A"
        )
    )
    g.add_node(
        PlanningNode(
            id="b", domain=PlanningDomain.BACKEND, kind=PlanningNodeKind.STORY, summary="B"
        )
    )
    g.add_edge(PlanningEdge(from_id="a", to_id="b", type=EdgeType.BLOCKS))
    g.add_edge(PlanningEdge(from_id="b", to_id="a", type=EdgeType.BLOCKS))
    is_valid, errors = validate_planning_graph(g)
    assert not is_valid
    assert any("cycle" in e.lower() for e in errors)


def test_format_validation_report():
    """Validation report formats correctly."""
    report = format_validation_report(
        is_valid=True,
        errors=[],
        total_nodes=5,
        total_edges=4,
        domain_counts={"backend": 3, "frontend": 2},
    )
    assert "PASSED" in report
    assert "5" in report
    assert "backend" in report
