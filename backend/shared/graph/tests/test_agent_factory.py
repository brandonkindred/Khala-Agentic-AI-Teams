"""Tests for shared.graph.agent_factory.build_agent — the cross-team Strands agent factory.

Run under ``LLM_PROVIDER=dummy`` (no real LLM, no Postgres): construction
resolves a dummy Strands model and never invokes it.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from strands import Agent

from shared.graph import build_agent


class _DummyStructuredOutput(BaseModel):
    value: str = "x"


def test_build_agent_json_mode() -> None:
    agent = build_agent(name="a1", system_prompt="do x", response_format="json")
    assert isinstance(agent, Agent)
    assert agent.name == "a1"


def test_build_agent_default_response_format_is_json() -> None:
    agent = build_agent(name="a1b", system_prompt="do x")
    assert isinstance(agent, Agent)


def test_build_agent_text_mode_with_description_and_tools() -> None:
    agent = build_agent(
        name="a2",
        system_prompt="do y",
        response_format="text",
        description="a helper",
        tools=[],
    )
    assert isinstance(agent, Agent)


def test_build_agent_structured_output() -> None:
    agent = build_agent(name="a3", system_prompt="do z", structured_output=_DummyStructuredOutput)
    assert isinstance(agent, Agent)


def test_build_agent_rejects_bad_response_format() -> None:
    with pytest.raises(ValueError, match="response_format must be"):
        build_agent(name="bad", system_prompt="x", response_format="xml")  # type: ignore[arg-type]


def test_build_agent_with_agent_key_override() -> None:
    agent = build_agent(name="a4", system_prompt="do w", agent_key="some_team")
    assert isinstance(agent, Agent)
    assert agent.name == "a4"


def _resolved_agent_key(agent: Agent) -> str | None:
    """Read back the ``agent_key`` that reached ``get_strands_model`` for *agent*.

    ``build_agent`` routes its ``agent_key`` argument through
    ``get_strands_model(agent_key, ...)`` into the ``LLMClientModel`` config;
    this recovers it the same way other teams verify per-agent routing — via
    the model's own ``get_config()``, not the ``build_agent`` call arguments,
    so a mistake inside ``build_agent``'s forwarding would also be caught.
    """
    return agent.model.get_config()["agent_key"]


def test_build_agent_forwards_agent_key_to_model_config() -> None:
    agent = build_agent(name="a5", system_prompt="do v", agent_key="some_team_tier")
    assert _resolved_agent_key(agent) == "some_team_tier"


def test_build_agent_default_agent_key_is_none() -> None:
    """Omitting ``agent_key`` falls back to ``None`` (the ``LLM_MODEL`` env var
    fallback), not any team-specific literal — this factory is team-agnostic."""
    agent = build_agent(name="a6", system_prompt="do u")
    assert _resolved_agent_key(agent) is None
