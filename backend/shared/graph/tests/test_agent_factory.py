"""Construction tests for ``shared.graph.build_agent``.

Run under ``LLM_PROVIDER=dummy`` (see ``conftest.py``): construction resolves
a dummy Strands model and never invokes it, so these call ``build_agent``
directly with no mocking, mirroring
``branding_team/tests/test_graphs.py``'s ``build_agent`` coverage.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands import Agent

from shared.graph import build_agent


class _DummyOutput:
    """Minimal stand-in for a Pydantic ``structured_output`` model."""


def _resolved_config(agent: Agent) -> dict[str, Any]:
    """Read back the config that reached ``get_strands_model`` for *agent*.

    ``build_agent`` routes ``agent_key``/``output_mode`` through
    ``get_strands_model(agent_key, response_format=output_mode)`` into the
    ``LLMClientModel`` config; recovering it via ``get_config()`` (rather than
    trusting the ``build_agent`` call arguments) also catches a mistake
    inside ``build_agent``'s forwarding.
    """
    return agent.model.get_config()


def test_build_agent_json_mode_default() -> None:
    agent = build_agent(name="a1", system_prompt="do x")
    assert isinstance(agent, Agent)
    assert agent.name == "a1"
    assert _resolved_config(agent)["response_format"] == "json"


def test_build_agent_explicit_json_mode() -> None:
    agent = build_agent(name="a2", system_prompt="do x", output_mode="json")
    assert _resolved_config(agent)["response_format"] == "json"


def _noop_tool() -> None:
    """Minimal stand-in for a Strands tool callable."""


def test_build_agent_text_mode_with_description_and_tools() -> None:
    agent = build_agent(
        name="a3",
        system_prompt="do y",
        output_mode="text",
        description="a helper",
        tools=[_noop_tool],
    )
    assert isinstance(agent, Agent)
    assert _resolved_config(agent)["response_format"] == "text"


def test_build_agent_rejects_bad_output_mode() -> None:
    with pytest.raises(ValueError, match="output_mode must be"):
        build_agent(name="bad", system_prompt="x", output_mode="xml")  # type: ignore[arg-type]


def test_build_agent_structured_output() -> None:
    agent = build_agent(name="a4", system_prompt="do z", structured_output=_DummyOutput)
    assert isinstance(agent, Agent)


def test_build_agent_with_agent_key_override() -> None:
    agent = build_agent(name="a5", system_prompt="do w", agent_key="some_team")
    assert isinstance(agent, Agent)
    assert _resolved_config(agent)["agent_key"] == "some_team"


def test_build_agent_default_agent_key_is_none() -> None:
    """Unlike branding's local wrapper (default ``"branding"``), the shared
    factory's own default is ``None`` — falling back to the bare
    ``LLM_MODEL`` env var. This test locks in that divergence."""
    agent = build_agent(name="a6", system_prompt="do v")
    assert _resolved_config(agent)["agent_key"] is None


def test_build_agent_callback_handler_passthrough() -> None:
    handler = object()
    agent = build_agent(name="a7", system_prompt="do u", callback_handler=handler)
    assert agent.callback_handler is handler
