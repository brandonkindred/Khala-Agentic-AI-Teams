"""Tests for the Strands Agents SDK adapter (``llm_service.strands_adapter``)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel

from llm_service.clients.dummy import DummyLLMClient
from llm_service.interface import LLMClient
from llm_service.strands_adapter import (
    LLMClientModel,
    _strands_messages_to_openai,
    _tool_specs_to_openai,
    get_strands_model,
    run_json_via_strands,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingClient(LLMClient):
    """Deterministic stub: records calls and returns a canned response.

    Lets us assert the exact payload the adapter handed to ``LLMClient`` and
    test both tool-call and plain-text branches of ``stream``.
    """

    def __init__(self, response: Dict[str, Any]) -> None:
        self.response = response
        self.chat_calls: List[Dict[str, Any]] = []
        self.complete_json_calls: List[Dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.complete_json_calls.append(
            {
                "prompt": prompt,
                "temperature": temperature,
                "system_prompt": system_prompt,
                "tools": tools,
                "think": think,
            }
        )
        return self.response

    def chat_json_round(
        self,
        messages: list,
        *,
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: bool = False,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.chat_calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "tools": tools,
                "think": think,
                "max_tokens": max_tokens,
            }
        )
        return self.response


def _drain(gen) -> List[Dict[str, Any]]:
    """Drain a Strands async stream into a list for easy assertions."""

    async def _run() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        async for event in gen:
            out.append(event)
        return out

    return asyncio.get_event_loop().run_until_complete(_run()) if False else asyncio.run(_run())


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def test_flatten_user_text_message() -> None:
    messages = [{"role": "user", "content": [{"text": "hello"}, {"text": "world"}]}]
    out = _strands_messages_to_openai(messages)
    assert out == [{"role": "user", "content": "hello\nworld"}]


def test_flatten_assistant_tool_use() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"text": "thinking"},
                {"toolUse": {"toolUseId": "t1", "name": "git_status", "input": {"staged": True}}},
            ],
        }
    ]
    out = _strands_messages_to_openai(messages)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "thinking"
    tool_calls = out[0]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "t1"
    assert tool_calls[0]["function"]["name"] == "git_status"
    # Arguments must be a JSON *string* for OpenAI-compatible chat APIs.
    assert isinstance(tool_calls[0]["function"]["arguments"], str)
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"staged": True}


def test_flatten_tool_result_emits_tool_role_message() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "content": [{"json": {"ok": True, "stdout": "clean"}}],
                    }
                }
            ],
        }
    ]
    out = _strands_messages_to_openai(messages)
    assert out == [
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": json.dumps({"ok": True, "stdout": "clean"}),
        }
    ]


def test_flatten_mixed_text_and_tool_result_splits_messages() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"text": "please use the result"},
                {"toolResult": {"toolUseId": "t9", "content": [{"text": "42"}]}},
            ],
        }
    ]
    out = _strands_messages_to_openai(messages)
    # Tool result flushed first (as its own message), then the remaining text.
    assert out[0] == {"role": "tool", "tool_call_id": "t9", "content": "42"}
    assert out[1] == {"role": "user", "content": "please use the result"}


def test_flatten_skips_unknown_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"text": "describe"},
                {"image": {"source": {"bytes": b"..."}}},  # unsupported, skipped
                {"reasoningContent": {"reasoningText": {"text": "..."}}},  # unsupported, skipped
            ],
        }
    ]
    out = _strands_messages_to_openai(messages)
    assert out == [{"role": "user", "content": "describe"}]


# ---------------------------------------------------------------------------
# Tool spec conversion
# ---------------------------------------------------------------------------


def test_tool_spec_conversion_unwraps_json_schema() -> None:
    specs = [
        {
            "name": "git_status",
            "description": "Check git status",
            "inputSchema": {
                "json": {"type": "object", "properties": {"staged": {"type": "boolean"}}}
            },
        }
    ]
    out = _tool_specs_to_openai(specs)
    assert out is not None
    assert len(out) == 1
    tool = out[0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "git_status"
    assert tool["function"]["description"] == "Check git status"
    assert tool["function"]["parameters"]["properties"] == {"staged": {"type": "boolean"}}


def test_tool_spec_conversion_accepts_bare_schema() -> None:
    # Forward-compat: allow an ``inputSchema`` that is already a plain dict
    # without the ``"json"`` wrapper.
    specs = [
        {
            "name": "list_files",
            "description": "List files",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    out = _tool_specs_to_openai(specs)
    assert out is not None
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_tool_spec_conversion_none() -> None:
    assert _tool_specs_to_openai(None) is None
    assert _tool_specs_to_openai([]) is None


# ---------------------------------------------------------------------------
# LLMClientModel.stream
# ---------------------------------------------------------------------------


def test_stream_emits_text_events_for_plain_response() -> None:
    client = _RecordingClient({"summary": "done", "status": "ok"})
    model = LLMClientModel(client, agent_key="qa_agent", temperature=0.1, think=True)

    events = _drain(
        model.stream(
            messages=[{"role": "user", "content": [{"text": "review this"}]}],
            system_prompt="You are a QA expert.",
        )
    )

    # Expected sequence: messageStart -> contentBlockStart -> contentBlockDelta -> contentBlockStop -> messageStop
    assert len(events) == 5
    assert events[0] == {"messageStart": {"role": "assistant"}}
    assert "contentBlockStart" in events[1]
    assert "contentBlockDelta" in events[2]
    assert "text" in events[2]["contentBlockDelta"]["delta"]
    # Dict responses are serialized to JSON so downstream consumers receive a stable string.
    assert json.loads(events[2]["contentBlockDelta"]["delta"]["text"]) == {
        "summary": "done",
        "status": "ok",
    }
    assert events[3] == {"contentBlockStop": {}}
    assert events[4] == {"messageStop": {"stopReason": "end_turn"}}

    # System prompt propagated to the LLMClient payload.
    assert len(client.chat_calls) == 1
    call = client.chat_calls[0]
    assert call["messages"][0] == {"role": "system", "content": "You are a QA expert."}
    assert call["messages"][1] == {"role": "user", "content": "review this"}
    assert call["temperature"] == 0.1
    assert call["think"] is True


def test_stream_emits_tool_use_events_for_tool_call_response() -> None:
    client = _RecordingClient(
        {
            "__tool_calls__": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "git_status", "arguments": {"staged": True}},
                }
            ]
        }
    )
    model = LLMClientModel(client)

    events = _drain(
        model.stream(
            messages=[{"role": "user", "content": [{"text": "check git"}]}],
            tool_specs=[
                {
                    "name": "git_status",
                    "description": "Show status",
                    "inputSchema": {"json": {"type": "object", "properties": {}}},
                }
            ],
        )
    )

    # messageStart + (contentBlockStart + delta + stop) per tool call + messageStop
    assert events[0] == {"messageStart": {"role": "assistant"}}
    start = events[1]["contentBlockStart"]["start"]
    assert start["toolUse"]["name"] == "git_status"
    assert start["toolUse"]["toolUseId"] == "call_1"
    delta = events[2]["contentBlockDelta"]["delta"]["toolUse"]
    # Arguments are always a JSON string so Strands can re-parse them.
    assert isinstance(delta["input"], str)
    assert json.loads(delta["input"]) == {"staged": True}
    assert events[3] == {"contentBlockStop": {}}
    assert events[4] == {"messageStop": {"stopReason": "tool_use"}}

    # Tools converted to OpenAI shape on the wire.
    assert len(client.chat_calls) == 1
    tools_sent = client.chat_calls[0]["tools"]
    assert tools_sent[0]["type"] == "function"
    assert tools_sent[0]["function"]["name"] == "git_status"


def test_stream_forwards_tool_result_messages_correctly() -> None:
    """Simulate the second round of a tool loop: user turn carries a toolResult."""
    client = _RecordingClient({"final": "done"})
    model = LLMClientModel(client)

    messages = [
        {"role": "user", "content": [{"text": "check git"}]},
        {
            "role": "assistant",
            "content": [
                {"toolUse": {"toolUseId": "t1", "name": "git_status", "input": {}}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "t1", "content": [{"json": {"clean": True}}]}}
            ],
        },
    ]

    _drain(model.stream(messages=messages))

    sent = client.chat_calls[0]["messages"]
    # Expected ordering: user text, assistant with tool_calls, tool response
    assert [m["role"] for m in sent] == ["user", "assistant", "tool"]
    assert sent[1]["tool_calls"][0]["function"]["name"] == "git_status"
    assert sent[2]["tool_call_id"] == "t1"
    assert json.loads(sent[2]["content"]) == {"clean": True}


def test_stream_per_call_overrides_via_invocation_state() -> None:
    client = _RecordingClient({"ok": True})
    model = LLMClientModel(client, temperature=0.0, think=False)

    _drain(
        model.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            invocation_state={"temperature": 0.9, "think": True, "max_tokens": 123},
        )
    )

    call = client.chat_calls[0]
    assert call["temperature"] == 0.9
    assert call["think"] is True
    assert call["max_tokens"] == 123


# ---------------------------------------------------------------------------
# LLMClientModel.structured_output
# ---------------------------------------------------------------------------


class _Review(BaseModel):
    summary: str
    approved: bool


def test_structured_output_validates_into_pydantic_model() -> None:
    client = _RecordingClient({"summary": "looks good", "approved": True})
    model = LLMClientModel(client, temperature=0.2, think=True)

    async def _run() -> Dict[str, Any]:
        async for event in model.structured_output(
            _Review,
            prompt=[{"role": "user", "content": [{"text": "Review this diff"}]}],
            system_prompt="You are a code reviewer.",
        ):
            return event
        raise AssertionError("no output")

    out = asyncio.run(_run())
    assert "output" in out
    review = out["output"]
    assert isinstance(review, _Review)
    assert review.summary == "looks good"
    assert review.approved is True

    # System prompt propagated; complete_json used (not chat_json_round).
    assert len(client.complete_json_calls) == 1
    assert client.complete_json_calls[0]["system_prompt"] == "You are a code reviewer."
    assert client.complete_json_calls[0]["temperature"] == 0.2
    assert client.complete_json_calls[0]["think"] is True


def test_structured_output_raises_on_invalid_response() -> None:
    client = _RecordingClient({"missing_fields": True})
    model = LLMClientModel(client)

    async def _run() -> None:
        async for _ in model.structured_output(
            _Review,
            prompt=[{"role": "user", "content": [{"text": "go"}]}],
        ):
            pass

    with pytest.raises(ValueError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Config + factory
# ---------------------------------------------------------------------------


def test_get_config_and_update_config() -> None:
    client = DummyLLMClient()
    model = LLMClientModel(client, agent_key="qa_agent", model_id="dummy-v1", temperature=0.3)
    cfg = model.get_config()
    assert cfg["agent_key"] == "qa_agent"
    assert cfg["model_id"] == "dummy-v1"
    assert cfg["temperature"] == 0.3

    model.update_config(temperature=0.0, think=True)
    cfg2 = model.get_config()
    assert cfg2["temperature"] == 0.0
    assert cfg2["think"] is True
    # Other fields untouched.
    assert cfg2["agent_key"] == "qa_agent"


def test_get_strands_model_with_injected_client_bypasses_factory() -> None:
    client = _RecordingClient({"status": "ok"})
    model = get_strands_model(agent_key="whatever", client=client, temperature=0.5)
    assert isinstance(model, LLMClientModel)
    assert model.get_config()["agent_key"] == "whatever"
    assert model.get_config()["temperature"] == 0.5

    _drain(model.stream(messages=[{"role": "user", "content": [{"text": "ping"}]}]))
    assert len(client.chat_calls) == 1


def test_get_strands_model_uses_dummy_client_when_provider_is_dummy(monkeypatch) -> None:
    from llm_service import factory

    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    factory._clear_client_cache_for_testing()

    model = get_strands_model(agent_key="test_agent")
    assert isinstance(model, LLMClientModel)
    # Backing client should be the DummyLLMClient selected by the factory.
    assert type(model._client).__name__ == "DummyLLMClient"


# ---------------------------------------------------------------------------
# End-to-end smoke test with DummyLLMClient's real chat_json_round
# ---------------------------------------------------------------------------


def test_stream_end_to_end_with_dummy_client_tool_loop() -> None:
    """Exercise the real DummyLLMClient, which returns a tool call on first round."""
    model = LLMClientModel(DummyLLMClient())
    tool_specs = [
        {
            "name": "git_status",
            "description": "Show status",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    ]
    events = _drain(
        model.stream(
            messages=[{"role": "user", "content": [{"text": "**Task:** demo"}]}],
            tool_specs=tool_specs,
        )
    )
    # First round: Dummy client emits a git_status tool call.
    stop = events[-1]["messageStop"]["stopReason"]
    assert stop == "tool_use"
    names = [
        e["contentBlockStart"]["start"]["toolUse"]["name"]
        for e in events
        if "contentBlockStart" in e
    ]
    assert names == ["git_status"]


# ---------------------------------------------------------------------------
# run_json_via_strands — the Wave 5 helper for defensively-parsed agents
# ---------------------------------------------------------------------------


def test_run_json_via_strands_returns_dict_from_dummy_stub() -> None:
    """Happy path: the helper routes through Strands + the dummy, returning
    the dict the dummy's pattern-match branch emits."""
    result = run_json_via_strands(
        DummyLLMClient(),
        system_prompt="You are a Software Architecture Expert.",
        user_prompt=(
            "Design an architecture. Produce JSON with keys: overview, "
            "architecture_document, components, diagrams, decisions."
        ),
        agent_key="architecture",
        temperature=0.1,
    )
    assert isinstance(result, dict)
    assert "overview" in result
    assert "components" in result
    assert len(result["components"]) >= 1


def test_run_json_via_strands_returns_empty_dict_on_exception() -> None:
    """If the backing client raises, the helper returns ``{}`` instead of
    propagating the exception — lets callers fall through to their
    ``data.get(...)`` defaults."""

    class _Broken(DummyLLMClient):
        def chat_json_round(self, *a: Any, **kw: Any) -> Dict[str, Any]:  # type: ignore[override]
            raise RuntimeError("simulated LLM failure")

        def complete_json(self, *a: Any, **kw: Any) -> Dict[str, Any]:  # type: ignore[override]
            raise RuntimeError("simulated LLM failure")

    result = run_json_via_strands(
        _Broken(),
        system_prompt="Anything",
        user_prompt="Anything",
    )
    assert result == {}


def test_run_json_via_strands_multiple_sequential_calls_succeed() -> None:
    """Regression: the helper constructs a fresh Strands Agent per call,
    so sequential invocations on the same client instance must not
    degrade. This is the Wave 1–4 state-leak guard applied to the Wave 5
    helper path."""
    client = DummyLLMClient()
    for i in range(4):
        # Architecture-shaped prompt — the user prompt carries the
        # ``overview`` + ``components`` + ``architecture_document`` tokens
        # the dummy routes on.
        result = run_json_via_strands(
            client,
            system_prompt="You are a Software Architecture Expert.",
            user_prompt=(
                f"Design architecture batch {i}. Produce JSON with keys: "
                "overview, architecture_document, components, diagrams, "
                "decisions."
            ),
            agent_key="architecture",
            temperature=0.1,
        )
        assert isinstance(result, dict), f"call {i} did not return a dict"
        assert "overview" in result, f"call {i} missing overview key"
        assert "components" in result, f"call {i} missing components key"
