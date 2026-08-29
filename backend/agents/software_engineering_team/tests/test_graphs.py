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


def test_review_result_protocol_runtime_checkable() -> None:
    """The ``ReviewResult`` Protocol should runtime-check ``approved: bool``."""
    from software_engineering_team.quality_gates.protocols import ReviewResult

    class _Approved:
        approved: bool = True

    class _NoAttr:
        pass

    assert isinstance(_Approved(), ReviewResult)
    assert not isinstance(_NoAttr(), ReviewResult)
