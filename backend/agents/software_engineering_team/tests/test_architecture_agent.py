"""Tests for Architecture Expert agent."""

import tempfile
from pathlib import Path

import pytest
from architecture_expert import ArchitectureExpertAgent, ArchitectureInput

from llm_service import DummyLLMClient
from software_engineering_team.shared.development_plan_writer import write_architecture_plan
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


def test_write_architecture_plan_includes_mermaid_diagrams(
    requirements: ProductRequirements,
) -> None:
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


def test_architecture_agent_builds_synthetic_when_parse_fails(
    requirements: ProductRequirements,
) -> None:
    """When the LLM returns unparseable content, the agent builds a
    synthetic architecture from requirements.

    After the Wave 5 migration the LLM call routes through
    ``run_json_via_strands``, which returns ``{}`` when the response text
    can't be parsed as JSON. The agent's ``not data.get("overview")`` check
    then triggers the synthetic-architecture fallback — same behavior as
    pre-migration, but now driven by the helper's parse-failure path
    rather than an ``LLMPermanentError``.
    """

    class _RawWrapperClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            # Return a dict that has *no* ``overview`` key — the agent's
            # ``is_parse_failure`` check (``not data.get("overview")``)
            # fires on this and triggers the synthetic fallback.
            return {"content": "Here is some non-JSON text from the model"}

    agent = ArchitectureExpertAgent(llm_client=_RawWrapperClient())
    result = agent.run(
        ArchitectureInput(requirements=requirements, technology_preferences=["Python", "FastAPI"])
    )
    assert result.architecture.overview
    assert "Task Manager API" in result.architecture.overview
    assert len(result.architecture.components) >= 1
    assert result.architecture.diagrams
    assert "client_server_architecture" in result.architecture.diagrams
    assert "security_architecture" in result.architecture.diagrams


def test_architecture_agent_multiple_sequential_runs_on_same_instance(
    requirements: ProductRequirements,
) -> None:
    """Regression: a single ``ArchitectureExpertAgent`` instance must
    handle many sequential ``run()`` calls. Wave 5 migrations route every
    LLM call through ``run_json_via_strands`` which builds a fresh Strands
    ``Agent`` per call, so this regression is avoided by construction."""
    agent = ArchitectureExpertAgent(llm_client=DummyLLMClient())
    for i in range(3):
        result = agent.run(
            ArchitectureInput(
                requirements=requirements, technology_preferences=["Python", "FastAPI"]
            )
        )
        assert result.architecture.overview, f"run {i} missing overview"
        assert len(result.architecture.components) >= 1, f"run {i} missing components"
