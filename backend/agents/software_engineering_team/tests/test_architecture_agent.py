"""Tests for Architecture Expert agent."""

import tempfile
from pathlib import Path

import pytest
from architecture_expert import ArchitectureExpertAgent, ArchitectureInput

from llm_service import DummyLLMClient
from shared.dev_models.models import ProductRequirements
from software_engineering_team.shared import llm as llm_mod
from software_engineering_team.shared.development_plan_writer import write_architecture_plan
from software_engineering_team.tests.conftest import _patch_fenced_response, _strands_model_double


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
    from shared.dev_models.models import SystemArchitecture

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

    The LLM call routes through ``complete_json_with_continuation``
    (``software_engineering_team.shared.llm``), which builds a fresh Strands
    ``Agent`` per call and returns the parsed JSON dict. Here the raw-wrapper
    client's response is valid JSON shaped as ``{"content": ...}`` — no parse
    exception occurs — so it's the agent's own ``is_parse_failure`` check
    (``not data.get("overview")``) that triggers the synthetic-architecture
    fallback, not ``complete_json_with_continuation``'s exception path.
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
    handle many sequential ``run()`` calls. Every ``run()`` call routes
    through ``complete_json_with_continuation``, which builds a fresh
    Strands ``Agent`` per call, so this regression is avoided by
    construction."""
    agent = ArchitectureExpertAgent(llm_client=DummyLLMClient())
    for i in range(3):
        result = agent.run(
            ArchitectureInput(
                requirements=requirements, technology_preferences=["Python", "FastAPI"]
            )
        )
        assert result.architecture.overview, f"run {i} missing overview"
        assert len(result.architecture.components) >= 1, f"run {i} missing components"


def test_architecture_agent_recovers_fenced_json_response(
    monkeypatch, requirements: ProductRequirements
) -> None:
    """A markdown-fenced LLM response is recovered via
    ``complete_json_with_continuation``'s ``extract_json_from_response``
    fallback instead of crashing. Before the migration to
    ``complete_json_with_continuation``, a fenced response made the bare
    ``json.loads`` raise an uncaught ``json.JSONDecodeError`` --
    ``except LLMPermanentError:`` does not catch that. The real LLM data
    must come through, not the synthetic requirements-only fallback.
    """
    payload = {
        "overview": "Fenced-response architecture overview for the task manager API.",
        "components": [
            {
                "name": "API Gateway",
                "type": "backend",
                "description": "Routes requests to services",
                "technology": "fastapi",
            },
        ],
        "architecture_document": "# Architecture\n\nFenced document body.",
        "diagrams": {"client_server_architecture": "flowchart TD\n  a --> b"},
        "decisions": [],
        "summary": "Fenced architecture summary.",
    }
    _patch_fenced_response(monkeypatch, payload)
    agent = ArchitectureExpertAgent(llm_client=_strands_model_double())
    result = agent.run(ArchitectureInput(requirements=requirements))
    assert result.architecture.overview == payload["overview"]
    assert len(result.architecture.components) == 1
    assert result.architecture.components[0].name == "API Gateway"
    assert result.summary == "Fenced architecture summary."


def test_architecture_agent_falls_back_to_synthetic_on_unrecoverable_response(
    monkeypatch, requirements: ProductRequirements
) -> None:
    """A genuinely unparseable response (no braces, no fences, no matching
    prose-prefix pattern) exhausts every ``extract_json_from_response``
    recovery strategy, so ``complete_json_with_continuation`` raises
    ``LLMJsonParseError``. This is the one scenario that actually drives the
    agent's own ``except LLMPermanentError:`` clause (a subclass
    relationship, not a bare re-raise) into the synthetic-architecture
    fallback -- distinct from both the shape-check fallback (valid JSON,
    wrong shape) and the fenced-recovery success path covered above.
    """

    class _UnparseableAgent:
        def __call__(self, prompt, **kwargs):
            return "I cannot produce a structured response for this request."

    monkeypatch.setattr(llm_mod, "Agent", lambda *a, **kw: _UnparseableAgent())
    agent = ArchitectureExpertAgent(llm_client=_strands_model_double())
    result = agent.run(ArchitectureInput(requirements=requirements))
    assert result.architecture.overview
    assert "Task Manager API" in result.architecture.overview
    assert len(result.architecture.components) >= 1


def test_architecture_agent_falls_back_to_synthetic_on_non_dict_recovery(
    monkeypatch, requirements: ProductRequirements
) -> None:
    """A fenced top-level JSON *array* (not an object) parses successfully
    via ``extract_json_from_response`` -- no exception is raised -- but the
    agent's own ``isinstance(data, dict)`` guard must still treat it as a
    parse failure and degrade to the synthetic-architecture fallback instead
    of crashing on ``data.get(...)``.
    """

    class _ArrayAgent:
        def __call__(self, prompt, **kwargs):
            return '```json\n["not", "an", "object"]\n```'

    monkeypatch.setattr(llm_mod, "Agent", lambda *a, **kw: _ArrayAgent())
    agent = ArchitectureExpertAgent(llm_client=_strands_model_double())
    result = agent.run(ArchitectureInput(requirements=requirements))
    assert result.architecture.overview
    assert "Task Manager API" in result.architecture.overview
    assert len(result.architecture.components) >= 1
