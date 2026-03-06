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


def test_find_assisting_agents_matches_required_skills_and_problem_keywords():
    matches = _registry().find_assisting_agents(
        problem_description="Need a UI accessibility review for component specs",
        required_skills=["accessibility_review"],
        limit=3,
    )
    assert matches
    top = matches[0]
    assert top["agent_id"] == "design_lead"
    assert "accessibility_review" in top["match"]["skills"]


def test_find_assisting_agents_returns_empty_without_matching_skills():
    matches = _registry().find_assisting_agents(
        problem_description="Need database sharding strategy",
        required_skills=["distributed_databases"],
        limit=3,
    )
    assert matches == []
