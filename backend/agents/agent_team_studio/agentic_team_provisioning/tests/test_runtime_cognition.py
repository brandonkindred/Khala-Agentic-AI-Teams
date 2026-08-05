"""Tests for the cognition-aware runtime in agent_builder.

Covers advisory-only prompt rendering, the writeback emitted under an open
channel, and no-op degradation when cognition is unavailable. The strands LLM is
mocked so no network call is made.
"""

from __future__ import annotations

import pytest

from agent_cognition.tools.channel import runtime_channel
from agent_team_studio.agentic_team_provisioning.runtime import agent_builder


class _FakeResult:
    """Mimics a strands AgentResult: ``.message`` is a structured dict and the
    text is only obtainable via ``str(result)`` (the SDK's content-join)."""

    def __init__(self, text: str) -> None:
        self.message = {"role": "assistant", "content": [{"text": text}]}
        self._text = text

    def __str__(self) -> str:
        return self._text


class _FakeStrandsAgent:
    """Records the system prompt + tools it was built with; echoes a fixed reply."""

    last_system_prompt: str | None = None
    last_tools: object = None

    def __init__(self, **kwargs) -> None:
        type(self).last_system_prompt = kwargs.get("system_prompt")
        type(self).last_tools = kwargs.get("tools")

    def __call__(self, message: str) -> _FakeResult:
        return _FakeResult("ok")


@pytest.fixture
def fake_strands(monkeypatch: pytest.MonkeyPatch):
    _FakeStrandsAgent.last_system_prompt = None
    _FakeStrandsAgent.last_tools = None
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


def test_resolve_tools_no_fallback_returns_empty():
    # The cognition/generated path disables the fallback so an agent never gets
    # _COMMON_TOOLS (network / code-exec) it didn't declare.
    assert agent_builder.resolve_tools([], allow_common_tools_fallback=False) == []
    assert agent_builder.resolve_tools(["nope"], allow_common_tools_fallback=False) == []
    # A recognized tool still resolves regardless of the fallback flag.
    assert agent_builder.resolve_tools(["http_request"], allow_common_tools_fallback=False) == [
        agent_builder.TOOL_REGISTRY["http_request"]
    ]


def test_cognition_path_grants_no_common_tools(fake_strands):
    # call_agent_with_cognition must not hand the agent the _COMMON_TOOLS fallback.
    agent_builder.call_agent_with_cognition("A", "r", [], [], [], [], "hi")
    assert fake_strands.last_tools == []


def test_call_agent_str_fallback():
    class _PlainAgent:
        def __call__(self, message: str) -> str:
            return "  plain text  "

    assert agent_builder.call_agent(_PlainAgent(), "hi") == "plain text"


def test_call_agent_extracts_text_not_message_dict():
    # Regression: a real AgentResult's `.message` is a structured dict; the text
    # must come from str(result), not str(result.message) (which is a dict repr).
    class _Result:
        message = {"role": "assistant", "content": [{"text": "the answer"}]}

        def __str__(self) -> str:
            return "the answer"

    class _Agent:
        def __call__(self, message: str):
            return _Result()

    assert agent_builder.call_agent(_Agent(), "hi") == "the answer"


def test_generate_starter_prompts_from_metadata():
    prompts = agent_builder.generate_starter_prompts("Agent X", "router", ["routing"], ["ops"])
    assert len(prompts) == 3
    assert any("router" in p for p in prompts)


def test_generate_starter_prompts_fallback_when_empty():
    prompts = agent_builder.generate_starter_prompts("Agent X", "", [], [])
    assert len(prompts) == 3
    assert any("Introduce yourself" in p for p in prompts)


@pytest.mark.asyncio
async def test_invoke_generated_agent_returns_output(fake_strands):
    result = await agent_builder.invoke_generated_agent(
        {"agent_name": "A", "role": "r", "message": "hello"}
    )
    assert result == {"output": "ok"}


@pytest.mark.asyncio
async def test_invoke_generated_agent_validates_body(fake_strands):
    from pydantic import ValidationError

    # Missing required fields (agent_name / message) → request-validation error,
    # surfaced as a clean ValidationError rather than a deep prompt-build crash.
    with pytest.raises(ValidationError):
        await agent_builder.invoke_generated_agent({})
    with pytest.raises(ValidationError):
        await agent_builder.invoke_generated_agent(None)
    # Wrong field types are rejected too (skills must be a list of strings).
    with pytest.raises(ValidationError):
        await agent_builder.invoke_generated_agent(
            {"agent_name": "A", "message": "hi", "skills": 1}
        )


@pytest.mark.asyncio
async def test_invoke_generated_agent_renders_cognition(fake_strands):
    with runtime_channel({"rules": [_ADVISORY]}):
        await agent_builder.invoke_generated_agent({"agent_name": "A", "message": "hi"})
    assert "Never reveal secrets." in fake_strands.last_system_prompt


@pytest.mark.asyncio
async def test_invoke_generated_agent_ignores_body_tools(fake_strands):
    # A caller-supplied recognized tool must NOT reach the agent — the generated
    # manifest declares no tools, so the runtime grants none (no python/http escalation).
    await agent_builder.invoke_generated_agent(
        {"agent_name": "A", "message": "hi", "tools": ["python", "http_request"]}
    )
    assert fake_strands.last_tools == []


@pytest.mark.asyncio
async def test_invoke_generated_agent_is_dispatch_compatible(fake_strands):
    # Regression: the manifest entrypoint must be invokable as ``fn(body)`` by the
    # sandbox dispatch shim. It's an async coroutine fn, so the shim awaits it; run
    # it through the real dispatch path to prove that contract end-to-end.
    from shared.agent_invoke.dispatch import invoke_entrypoint

    result = await invoke_entrypoint(
        "agent_team_studio.agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent",
        {"agent_name": "A", "message": "hi"},
    )
    # No channel open → plain output, no writeback envelope.
    assert result == {"output": "ok"}


def test_shape_invoke_result_plain_without_writeback():
    assert agent_builder._shape_invoke_result("hi", None) == {"output": "hi"}
    assert agent_builder._shape_invoke_result("hi", {}) == {"output": "hi"}


def test_shape_invoke_result_wraps_writeback():
    from agent_cognition.tools.envelope import WRITEBACK_MARKER, try_unwrap_writeback

    wb = {"events": [{"id": "e1"}], "tool_calls": []}
    wrapped = agent_builder._shape_invoke_result("hi", wb)
    assert wrapped[WRITEBACK_MARKER] == 1
    unwrapped = try_unwrap_writeback(wrapped)
    assert unwrapped.output == {"output": "hi"}
    assert unwrapped.events == [{"id": "e1"}]


def test_shape_invoke_result_degrades_when_envelope_absent(monkeypatch: pytest.MonkeyPatch):
    # If the cognition package can't be imported, fall back to plain output.
    monkeypatch.setitem(__import__("sys").modules, "agent_cognition.tools.envelope", None)
    assert agent_builder._shape_invoke_result("hi", {"events": [1]}) == {"output": "hi"}


@pytest.mark.asyncio
async def test_invoke_generated_agent_writeback_reaches_shim(fake_strands):
    # End-to-end: with a channel open, invoke_generated_agent returns a wrapped
    # writeback whose events the shim lifts into its driven result (→ memory_events).
    from shared.agent_invoke.cognition_envelope import maybe_drive_tool_loop

    with runtime_channel({"rules": [_ADVISORY]}):
        result = await agent_builder.invoke_generated_agent({"agent_name": "A", "message": "hi"})

    driven = maybe_drive_tool_loop(result, agent_id="A", source_run_id="r", cognition=None)
    assert driven["output"] == {"output": "ok"}
    assert len(driven["events"]) == 1
    assert driven["events"][0]["kind"] == "outcome"
