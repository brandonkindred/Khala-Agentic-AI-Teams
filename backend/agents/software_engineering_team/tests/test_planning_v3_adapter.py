"""Unit tests for planning_v3_adapter: adapt Planning V3 handoff to Tech Lead/Architecture inputs."""

from pathlib import Path

import pytest

from planning_v3_adapter import adapt_planning_v3_result, PlanningV2AdapterResult


def test_adapt_planning_v3_result_success_with_handoff() -> None:
    """adapt_planning_v3_result with success=True and handoff_package returns ProductRequirements and project_overview; hierarchy is None."""
    result = {
        "success": True,
        "handoff_package": {
            "validated_spec_content": "Validated spec for the project.",
            "prd_content": "## PRD\nProduct requirements document.",
            "client_context": {
                "problem_summary": "Users need a task manager.",
                "opportunity_statement": "Simplify daily planning.",
                "target_users": ["Individuals", "Teams"],
                "success_criteria": ["Launch on time", "High adoption"],
                "assumptions": ["Web-first"],
            },
            "summary": "Handoff complete.",
        },
        "failure_reason": None,
    }
    out = adapt_planning_v3_result(result, spec_title="My Project")
    assert isinstance(out, PlanningV2AdapterResult)
    assert out.requirements.title == "My Project"
    assert "Validated spec" in out.requirements.description
    assert "PRD" in out.requirements.description
    assert out.requirements.acceptance_criteria == ["Launch on time", "High adoption"]
    assert "features_and_functionality_doc" in out.project_overview
    assert "task manager" in out.project_overview["features_and_functionality_doc"]
    assert out.hierarchy is None
    assert out.final_spec_content == "Validated spec for the project."
    assert out.open_questions == []
    assert out.assumptions == ["Web-first"]


def test_adapt_planning_v3_result_raises_when_success_false() -> None:
    """adapt_planning_v3_result raises ValueError when result.success is False or missing."""
    with pytest.raises(ValueError, match="Workflow failed"):
        adapt_planning_v3_result({"success": False, "failure_reason": "Workflow failed."}, spec_title="X")
    with pytest.raises(ValueError):
        adapt_planning_v3_result({"failure_reason": "No success key."}, spec_title="X")


def test_adapt_planning_v3_result_prd_fallback_from_repo_path(tmp_path: Path) -> None:
    """When handoff has no prd_content but repo_path has plan/product_analysis/product_requirements_document.md, description uses PRD content."""
    prd_dir = tmp_path / "plan" / "product_analysis"
    prd_dir.mkdir(parents=True)
    prd_file = prd_dir / "product_requirements_document.md"
    prd_file.write_text("# PRD from disk\n\nMust ship by Q2.", encoding="utf-8")

    result = {
        "success": True,
        "handoff_package": {
            "validated_spec_content": "Spec content.",
            "prd_content": None,
            "client_context": {"success_criteria": []},
            "summary": "",
        },
    }
    out = adapt_planning_v3_result(result, spec_title="Project", repo_path=str(tmp_path))
    assert "PRD from disk" in out.requirements.description
    assert "Must ship by Q2" in out.requirements.description
