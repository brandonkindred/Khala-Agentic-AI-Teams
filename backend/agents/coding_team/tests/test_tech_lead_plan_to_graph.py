"""Unit test for Tech Lead plan-to-Task-Graph: given CodingTeamPlanInput, output has tasks with deps and StackSpec list."""

from __future__ import annotations

from unittest.mock import MagicMock

from coding_team.models import CodingTeamPlanInput
from coding_team.tech_lead_agent.agent import TechLeadAgent


def test_tech_lead_plan_to_task_graph_output_structure() -> None:
    """Given CodingTeamPlanInput, Tech Lead output contains tasks (with deps) and stacks list."""
    plan = CodingTeamPlanInput(
        requirements_title="Test Project",
        requirements_description="Build a small API and UI.",
        project_overview={"features_and_functionality_doc": "REST API", "goals": "Ship fast"},
        final_spec_content="Spec content here.",
        repo_path="/tmp/repo",
        architecture_overview="Backend FastAPI, frontend Angular.",
    )
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "tasks": [
            {"id": "t1", "title": "Backend API", "description": "Implement endpoints", "dependencies": []},
            {"id": "t2", "title": "Frontend UI", "description": "Implement UI", "dependencies": ["t1"]},
        ],
        "stacks": [
            {"name": "backend", "tools_services": ["Python", "FastAPI"]},
            {"name": "frontend", "tools_services": ["Angular", "TypeScript"]},
        ],
    }
    agent = TechLeadAgent(llm=mock_llm)
    out = agent.run_plan_to_task_graph(plan)
    assert "tasks" in out
    assert "stacks" in out
    tasks = out["tasks"]
    stacks = out["stacks"]
    assert len(tasks) == 2
    assert tasks[0]["id"] == "t1"
    assert tasks[0]["dependencies"] == []
    assert tasks[1]["id"] == "t2"
    assert tasks[1]["dependencies"] == ["t1"]
    assert len(stacks) == 2
    assert stacks[0]["name"] == "backend"
    assert "FastAPI" in stacks[0]["tools_services"]
    assert stacks[1]["name"] == "frontend"


def test_tech_lead_plan_to_task_graph_llm_failure_returns_defaults() -> None:
    """When LLM fails, return empty tasks and default stack."""
    plan = CodingTeamPlanInput(
        requirements_title="X",
        requirements_description="",
        repo_path="/tmp",
    )
    mock_llm = MagicMock()
    mock_llm.complete_json.side_effect = RuntimeError("LLM error")
    agent = TechLeadAgent(llm=mock_llm)
    out = agent.run_plan_to_task_graph(plan)
    assert out["tasks"] == []
    assert len(out["stacks"]) == 1
    assert out["stacks"][0]["name"] == "default"
