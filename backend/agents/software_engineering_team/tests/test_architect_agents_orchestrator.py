"""Characterization tests for architect_agents/agents/orchestrator.py.

create_orchestrator() is a pure factory with no error handling of its own —
these tests pin down its current behavior (prompt loading, model resolution,
tools composition, session_manager forwarding, and uncaught failure
propagation) as-is, independent of any framework/prompt-format migration.
"""

import agents.orchestrator as orchestrator_mod
import pytest
from agents.api_design import api_design_architect
from agents.application import application_architect
from agents.architecture_scrutineer import architecture_scrutineer
from agents.cloud_infra import cloud_infrastructure_architect
from agents.data import data_architect
from agents.data_streaming import data_streaming_architect
from agents.devops import devops_architect
from agents.observability import observability_architect
from agents.orchestrator import create_orchestrator
from agents.prompts import ORCHESTRATOR_PROMPT
from agents.security import security_architect
from tools import aws_pricing_tool, document_writer_tool, file_read_tool, web_search_tool

EXPECTED_TOOLS = [
    file_read_tool,
    aws_pricing_tool,
    web_search_tool,
    document_writer_tool,
    security_architect,
    application_architect,
    data_architect,
    api_design_architect,
    cloud_infrastructure_architect,
    data_streaming_architect,
    devops_architect,
    observability_architect,
    architecture_scrutineer,
]


class _FakeAgent:
    """Stands in for strands.Agent, capturing the kwargs it was built with."""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        _FakeAgent.last_kwargs = kwargs
        self.kwargs = kwargs


class _FakeAgentBoom:
    """Stands in for strands.Agent, always failing construction."""

    def __init__(self, **kwargs):
        raise RuntimeError("boom")


@pytest.fixture
def fake_agent(monkeypatch):
    _FakeAgent.last_kwargs = None
    monkeypatch.setattr(orchestrator_mod, "Agent", _FakeAgent)
    return _FakeAgent


def test_create_orchestrator_returns_configured_agent(fake_agent) -> None:
    result = create_orchestrator()

    assert isinstance(result, fake_agent)
    kwargs = fake_agent.last_kwargs
    assert isinstance(kwargs["system_prompt"], str)
    assert "Enterprise Architect Orchestrator" in kwargs["system_prompt"]


def test_create_orchestrator_default_model_env(monkeypatch, fake_agent) -> None:
    monkeypatch.delenv("ARCHITECT_MODEL_ORCHESTRATOR", raising=False)

    create_orchestrator()

    assert fake_agent.last_kwargs["model"] == "anthropic.claude-opus-4-6-v1"


def test_create_orchestrator_model_env_override(monkeypatch, fake_agent) -> None:
    monkeypatch.setenv("ARCHITECT_MODEL_ORCHESTRATOR", "custom.model.id")

    create_orchestrator()

    assert fake_agent.last_kwargs["model"] == "custom.model.id"


def test_create_orchestrator_prompt_matches_constant(fake_agent) -> None:
    create_orchestrator()

    assert fake_agent.last_kwargs["system_prompt"] == ORCHESTRATOR_PROMPT


def test_create_orchestrator_tools_list_composition(fake_agent) -> None:
    create_orchestrator()

    assert fake_agent.last_kwargs["tools"] == EXPECTED_TOOLS


def test_create_orchestrator_callback_handler_always_none(fake_agent) -> None:
    create_orchestrator()

    assert fake_agent.last_kwargs["callback_handler"] is None


def test_create_orchestrator_session_manager_omitted_when_none(fake_agent) -> None:
    create_orchestrator(session_manager=None)

    assert "session_manager" not in fake_agent.last_kwargs


def test_create_orchestrator_session_manager_forwarded_when_provided(fake_agent) -> None:
    sentinel = object()

    create_orchestrator(session_manager=sentinel)

    assert fake_agent.last_kwargs["session_manager"] is sentinel


def test_create_orchestrator_agent_construction_failure_propagates(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_mod, "Agent", _FakeAgentBoom)

    with pytest.raises(RuntimeError, match="boom"):
        create_orchestrator()
