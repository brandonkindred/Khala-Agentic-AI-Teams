"""Tests for Architecture Expert agent."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from architecture_expert import ArchitectureExpertAgent, ArchitectureInput
from software_engineering_team.shared.development_plan_writer import write_architecture_plan
from software_engineering_team.shared.llm import DummyLLMClient
from software_engineering_team.shared.models import ProductRequirements


@pytest.fixture
def requirements() -> ProductRequirements:
    return ProductRequirements(
        title="Task Manager API",
        description="REST API for tasks with CRUD",
        acceptance_criteria=["POST /tasks", "GET /tasks"],
        constraints=["Python FastAPI", "PostgreSQL"],
        priority="high",
    )


def test_architecture_agent_produces_components(requirements: ProductRequirements) -> None:
    """Architecture Expert returns SystemArchitecture with components."""
    llm = DummyLLMClient()
    agent = ArchitectureExpertAgent(llm_client=llm)
    result = agent.run(
        ArchitectureInput(
            requirements=requirements,
            technology_preferences=["Python", "FastAPI"],
        )
    )
    assert result.architecture.overview
    assert len(result.architecture.components) >= 1
    assert any(c.type == "backend" for c in result.architecture.components)
    assert result.summary or result.architecture.architecture_document


def test_architecture_agent_with_existing_architecture(requirements: ProductRequirements) -> None:
    """Architecture Expert accepts existing_architecture for extension."""
    from software_engineering_team.shared.models import SystemArchitecture
    llm = DummyLLMClient()
    agent = ArchitectureExpertAgent(llm_client=llm)
    existing = SystemArchitecture(
        overview="Existing API",
        components=[],
    )
    result = agent.run(
        ArchitectureInput(
            requirements=requirements,
            existing_architecture=existing.overview,
        )
    )
    assert result.architecture.components


def test_architecture_agent_produces_diagrams(requirements: ProductRequirements) -> None:
    """Architecture Expert returns diagrams when using DummyLLMClient."""
    llm = DummyLLMClient()
    agent = ArchitectureExpertAgent(llm_client=llm)
    result = agent.run(
        ArchitectureInput(requirements=requirements, technology_preferences=["Python", "FastAPI"])
    )
    assert result.architecture.diagrams
    assert "client_server_architecture" in result.architecture.diagrams
    assert "frontend_code_structure" in result.architecture.diagrams


def test_write_architecture_plan_includes_mermaid_diagrams(requirements: ProductRequirements) -> None:
    """Written architecture plan contains Diagrams section and Mermaid code blocks."""
    llm = DummyLLMClient()
    agent = ArchitectureExpertAgent(llm_client=llm)
    result = agent.run(
        ArchitectureInput(requirements=requirements, technology_preferences=["Python", "FastAPI"])
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_architecture_plan(Path(tmpdir), result.architecture)
        content = path.read_text()
    assert "## Diagrams" in content
    assert "```mermaid" in content


def test_architecture_agent_builds_synthetic_when_parse_fails(requirements: ProductRequirements) -> None:
    """When LLM returns raw wrapper (parse failure), agent builds synthetic architecture."""
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {"content": "Here is some non-JSON text from the model"}
    agent = ArchitectureExpertAgent(llm_client=mock_llm)
    result = agent.run(
        ArchitectureInput(requirements=requirements, technology_preferences=["Python", "FastAPI"])
    )
    assert result.architecture.overview
    assert "Task Manager API" in result.architecture.overview
    assert len(result.architecture.components) >= 1
    assert result.architecture.diagrams
    assert "client_server_architecture" in result.architecture.diagrams
    assert "security_architecture" in result.architecture.diagrams
