"""Tests for planning template parser (template-based LLM output)."""


from planning_team.planning_template import parse_planning_template


def test_parse_planning_template_extracts_nodes_and_edges() -> None:
    """Template with NODES and EDGES sections parses to lists."""
    text = """## NODES ##
id: backend-api
domain: backend
kind: epic
summary: Backend API
details: REST API for the app.
acceptance_criteria: AC1 | AC2
---
id: backend-crud
domain: backend
kind: task
summary: CRUD endpoints
details: Implement CRUD.
---
## END NODES ##
## EDGES ##
from_id: backend-api
to_id: backend-crud
type: blocks
---
## END EDGES ##
## SUMMARY ##
Backend plan with 2 nodes.
## END SUMMARY ##
"""
    out = parse_planning_template(text)
    assert len(out["nodes"]) == 2
    assert out["nodes"][0]["id"] == "backend-api"
    assert out["nodes"][0]["summary"] == "Backend API"
    assert out["nodes"][0]["acceptance_criteria"] == ["AC1", "AC2"]
    assert out["nodes"][1]["id"] == "backend-crud"
    assert len(out["edges"]) == 1
    assert out["edges"][0]["from_id"] == "backend-api"
    assert out["edges"][0]["to_id"] == "backend-crud"
    assert out["summary"] == "Backend plan with 2 nodes."


def test_parse_planning_template_tolerates_truncation() -> None:
    """Missing ## END NODES ## still yields nodes (truncation-tolerant)."""
    text = """## NODES ##
id: only-node
domain: backend
kind: task
summary: Only one
details: Truncated below.
---
"""
    out = parse_planning_template(text)
    assert len(out["nodes"]) == 1
    assert out["nodes"][0]["id"] == "only-node"
    assert out["edges"] == []
    assert out["summary"] == ""


def test_parse_planning_template_empty_sections() -> None:
    """Empty sections return empty lists. Markers must be at line start (no leading space)."""
    text = (
        "## NODES ##\n"
        "## END NODES ##\n"
        "## EDGES ##\n"
        "## END EDGES ##\n"
        "## SUMMARY ##\n"
        "## END SUMMARY ##\n"
    )
    out = parse_planning_template(text)
    assert out["nodes"] == []
    assert out["edges"] == []
    assert out["summary"] == ""
