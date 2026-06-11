"""Tests for the cognition-aware runtime in agent_builder.

Covers advisory-only prompt rendering, the writeback emitted under an open
channel, and no-op degradation when cognition is unavailable. The strands LLM is
mocked so no network call is made.
"""

from __future__ import annotations

import pytest

from agent_cognition.tools.channel import runtime_channel
from agentic_team_provisioning.runtime import agent_builder


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.message = text


class _FakeStrandsAgent:
    """Records the system prompt it was built with; echoes a fixed reply."""

    last_system_prompt: str | None = None

    def __init__(self, **kwargs) -> None:
        type(self).last_system_prompt = kwargs.get("system_prompt")

    def __call__(self, message: str) -> _FakeResult:
        return _FakeResult("ok")


@pytest.fixture
def fake_strands(monkeypatch: pytest.MonkeyPatch):
    _FakeStrandsAgent.last_system_prompt = None
    monkeypatch.setattr(agent_builder, "StrandsAgent", _FakeStrandsAgent)
    return _FakeStrandsAgent


_ADVISORY = {"text": "Never reveal secrets.", "mode": "advisory", "priority": 100}
_ENFORCED = {"text": "ENFORCED-ONLY rule.", "mode": "enforced", "priority": 50}


def test_render_cognition_prompt_advisory_only():
    cognition = {"rules": [_ADVISORY, _ENFORCED], "memory_digest": "Past run: did X."}
    out = agent_builder.render_cognition_prompt("BASE", cognition)
    assert "BASE" in out
    assert "Never reveal secrets." in out
    assert "ENFORCED-ONLY rule." not in out  # enforced rules never rendered here
    assert "Past run: did X." in out


def test_render_cognition_prompt_orders_by_priority():
    low = {"text": "low-pri", "mode": "advisory", "priority": 1}
    high = {"text": "high-pri", "mode": "advisory", "priority": 99}
    out = agent_builder.render_cognition_prompt("BASE", {"rules": [low, high]})
    assert out.index("high-pri") < out.index("low-pri")


def test_render_cognition_prompt_noop_when_none_or_empty():
    assert agent_builder.render_cognition_prompt("BASE", None) == "BASE"
    assert agent_builder.render_cognition_prompt("BASE", {}) == "BASE"
    # An enforced-only block with no digest contributes nothing renderable.
    assert agent_builder.render_cognition_prompt("BASE", {"rules": [_ENFORCED]}) == "BASE"


def test_call_agent_with_cognition_renders_and_writes_back(fake_strands):
    cognition = {"rules": [_ADVISORY], "memory_digest": "remember this"}
    with runtime_channel(cognition):
        text, writeback = agent_builder.call_agent_with_cognition(
            "A", "role", [], [], [], [], "hello", agent_id="agent-1"
        )

    assert text == "ok"
    # The advisory rule was folded into the agent's system prompt.
    assert "Never reveal secrets." in fake_strands.last_system_prompt
    assert "remember this" in fake_strands.last_system_prompt
    # A writeback with one outcome event was emitted.
    assert writeback is not None
    assert len(writeback["events"]) == 1
    assert writeback["events"][0]["kind"] == "outcome"
    assert writeback["events"][0]["agent_id"] == "agent-1"


def test_call_agent_with_cognition_noop_without_channel(fake_strands):
    # No runtime_channel open → no steering, no writeback.
    text, writeback = agent_builder.call_agent_with_cognition(
        "A", "role", ["s1"], [], [], [], "hello"
    )
    assert text == "ok"
    assert writeback is None
    # The system prompt is exactly the base prompt (no guidance section).
    assert "Operating guidance" not in fake_strands.last_system_prompt


def test_read_cognition_context_degrades_when_package_absent(monkeypatch: pytest.MonkeyPatch):
    # Force the lazy import to fail → returns None instead of raising.
    monkeypatch.setitem(__import__("sys").modules, "agent_cognition.tools.channel", None)
    assert agent_builder._read_cognition_context() is None


def test_build_writeback_degrades_when_models_absent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(__import__("sys").modules, "agent_cognition.models", None)
    assert agent_builder._build_writeback("agent-1", "text") is None


def test_build_writeback_happy_path():
    wb = agent_builder._build_writeback("agent-9", "a response")
    assert wb is not None
    assert wb["events"][0]["kind"] == "outcome"
    assert wb["events"][0]["source_run_id"] == "agent-9#local"


def test_build_system_prompt_includes_all_sections():
    prompt = agent_builder.build_system_prompt(
        "Agent X",
        "do things",
        skills=["analysis"],
        capabilities=["search"],
        tools=["Git"],
        expertise=["support"],
    )
    assert "Skills: analysis" in prompt
    assert "Capabilities: search" in prompt
    assert "Expertise: support" in prompt
    assert "Available tools: Git" in prompt


def test_resolve_tools_maps_known_and_ignores_unknown():
    resolved = agent_builder.resolve_tools(["http_request", "Totally Unknown Tool"])
    # The known tool resolves; the unknown one is dropped (not the common fallback).
    assert resolved == [agent_builder.TOOL_REGISTRY["http_request"]]


def test_resolve_tools_falls_back_to_common_when_none_resolve():
    resolved = agent_builder.resolve_tools(["nope"])
    assert resolved == agent_builder._COMMON_TOOLS


def test_call_agent_str_fallback():
    class _PlainAgent:
        def __call__(self, message: str) -> str:
            return "  plain text  "

    assert agent_builder.call_agent(_PlainAgent(), "hi") == "plain text"


def test_generate_starter_prompts_from_metadata():
    prompts = agent_builder.generate_starter_prompts("Agent X", "router", ["routing"], ["ops"])
    assert len(prompts) == 3
    assert any("router" in p for p in prompts)


def test_generate_starter_prompts_fallback_when_empty():
    prompts = agent_builder.generate_starter_prompts("Agent X", "", [], [])
    assert len(prompts) == 3
    assert any("Introduce yourself" in p for p in prompts)
