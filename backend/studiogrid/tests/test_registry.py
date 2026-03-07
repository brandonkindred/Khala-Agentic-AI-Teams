from pathlib import Path

from studiogrid.runtime.registry_loader import RegistryLoader


def _registry() -> RegistryLoader:
    return RegistryLoader(Path(__file__).resolve().parents[1] / "src" / "studiogrid")


def test_registry_lists_agents_with_metadata():
    agents = _registry().list_agents()
    assert agents
    first = agents[0]
    assert "agent_id" in first
    assert "description" in first
    assert isinstance(first.get("skills", []), list)


def test_find_assisting_agents_prefers_same_team_for_requesting_agent():
    matches = _registry().find_assisting_agents(
        problem_description="Need an accessibility review for the handoff",
        required_skills=["accessibility_review"],
        requesting_agent_id="design_lead",
        limit=3,
    )
    assert matches
    top = matches[0]
    assert top["agent_id"] == "design_lead"
    assert top["match"]["shared_teams"] == ["product_design"]


def test_find_assisting_agents_returns_empty_without_matching_skills():
    matches = _registry().find_assisting_agents(
        problem_description="Need database sharding strategy",
        required_skills=["distributed_databases"],
        limit=3,
    )
    assert matches == []


def test_list_teams_filters_by_availability():
    teams = _registry().list_teams(available_only=True)
    assert teams == [
        {
            "team_id": "product_design",
            "description": "Team focused on UX and design-system delivery.",
            "is_available": True,
            "agent_ids": ["design_lead"],
        }
    ]
