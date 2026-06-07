"""Unit test for Tech Lead plan-to-Task-Graph: given CodingTeamPlanInput, output has tasks with deps and StackSpec list."""

from __future__ import annotations

from coding_team.models import CodingTeamPlanInput
from coding_team.tech_lead_agent import agent as tl_mod
from coding_team.tech_lead_agent.agent import TechLeadAgent


def test_tech_lead_plan_to_task_graph_output_structure(monkeypatch) -> None:
    """Given CodingTeamPlanInput, Tech Lead output contains tasks (with deps) and stacks list."""
    plan = CodingTeamPlanInput(
        requirements_title="Test Project",
        requirements_description="Build a small API and UI.",
        project_overview={"features_and_functionality_doc": "REST API", "goals": "Ship fast"},
        final_spec_content="Spec content here.",
        repo_path="/tmp/repo",
        architecture_overview="Backend FastAPI, frontend Angular.",
    )
    # The Tech Lead drives a strands Agent via _agent_call_json; stub both so no real LLM runs.
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt: {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Backend API",
                    "description": "Implement endpoints",
                    "dependencies": [],
                },
                {
                    "id": "t2",
                    "title": "Frontend UI",
                    "description": "Implement UI",
                    "dependencies": ["t1"],
                },
            ],
            "stacks": [
                {"name": "backend", "tools_services": ["Python", "FastAPI"]},
                {"name": "frontend", "tools_services": ["Angular", "TypeScript"]},
            ],
        },
    )
    agent = TechLeadAgent(model=object())
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


def test_tech_lead_run_groom_task(monkeypatch) -> None:
    """run_groom_task returns the enriched grooming fields parsed from the LLM JSON."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda agent, prompt: {
            "acceptance_criteria": ["done when tests pass"],
            "out_of_scope": "no UI",
            "description_enriched": "more detail",
            "priority": "high",
            "subtasks": [{"id": "s1", "title": "sub", "description": "", "dependencies": []}],
            "task_dependencies": ["d1"],
        },
    )
    out = TechLeadAgent(model=object()).run_groom_task("t1", "T", "desc", ["d1"], "plan ctx")
    assert out["acceptance_criteria"] == ["done when tests pass"]
    assert out["priority"] == "high"
    assert out["subtasks"][0]["id"] == "s1"
    assert out["task_dependencies"] == ["d1"]


def test_tech_lead_run_groom_task_llm_failure_returns_defaults(monkeypatch) -> None:
    """When grooming's LLM call fails, return safe defaults that preserve the input dependencies."""
    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())

    def boom(agent, prompt):
        raise RuntimeError("LLM error")

    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    out = TechLeadAgent(model=object()).run_groom_task("t1", "T", "desc", ["d1"], "ctx")
    assert out["priority"] == "medium"
    assert out["acceptance_criteria"] == []
    assert out["task_dependencies"] == ["d1"]


def test_tech_lead_plan_to_task_graph_llm_failure_returns_defaults(monkeypatch) -> None:
    """When the LLM call fails, return empty tasks and a single default stack."""
    plan = CodingTeamPlanInput(
        requirements_title="X",
        requirements_description="",
        repo_path="/tmp",
    )

    def boom(agent, prompt):
        raise RuntimeError("LLM error")

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    agent = TechLeadAgent(model=object())
    out = agent.run_plan_to_task_graph(plan)
    assert out["tasks"] == []
    assert len(out["stacks"]) == 1
    assert out["stacks"][0]["name"] == "default"


def test_plan_text_passes_all_fields_uncut() -> None:
    """Every plan field reaches the Task-Graph prompt in full — inputs are never truncated."""
    big_desc = "D" * 50000
    big_overview_value = "O" * 50000
    big_spec = "S" * 50000
    big_arch = "A" * 50000
    plan = CodingTeamPlanInput(
        requirements_title="Big Project",
        requirements_description=big_desc,
        project_overview={"doc": big_overview_value},
        final_spec_content=big_spec,
        repo_path="/tmp/repo",
        architecture_overview=big_arch,
    )
    text = tl_mod._plan_text(plan)
    assert big_desc in text
    assert big_overview_value in text
    assert big_spec in text
    assert big_arch in text


def test_run_groom_task_passes_full_inputs_to_llm(monkeypatch) -> None:
    """A large task description and plan context reach the groom prompt in full, never truncated."""
    captured: dict[str, str] = {}

    def _capture(agent, prompt):
        captured["prompt"] = prompt
        return {}

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(tl_mod, "_agent_call_json", _capture)
    agent = TechLeadAgent(model=object())
    big_desc = "D" * 50000
    big_context = "C" * 50000

    agent.run_groom_task(
        task_id="t1",
        task_title="T1",
        task_description=big_desc,
        task_dependencies=[],
        plan_context=big_context,
    )

    assert big_desc in captured["prompt"]
    assert big_context in captured["prompt"]
