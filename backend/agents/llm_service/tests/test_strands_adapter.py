"""Tests for the Strands Agents SDK adapter (``llm_service.strands_adapter``)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel

from llm_service.clients.dummy import DummyLLMClient
from llm_service.interface import (
    LLMClient,
    record_complete_json_turn,
    reset_complete_json_observer_state,
    take_complete_json_turns,
)
from llm_service.strands_adapter import (
    LLMClientModel,
    _flatten_system_prompt_content,
    _strands_messages_to_openai,
    _tool_specs_to_openai,
    run_json_via_strands,
)
from llm_service.strands_adapter import (
    _get_strands_model as get_strands_model,
)


@pytest.fixture(autouse=True)
def _reset_observer_turns() -> None:
    reset_complete_json_observer_state()
    yield
    reset_complete_json_observer_state()


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
        structured_output_model: Optional[type] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.complete_json_calls.append(
            {
                "prompt": prompt,
                "temperature": temperature,
                "system_prompt": system_prompt,
                "tools": tools,
                "think": think,
                "structured_output_model": structured_output_model,
            }
        )
        return self.response

    def chat(
        self,
        messages: list,
        *,
        response_format: str = "json",
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: bool = False,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        call = {
            "messages": messages,
            "response_format": response_format,
            "temperature": temperature,
            "tools": tools,
            "think": think,
            "max_tokens": max_tokens,
        }
        self.chat_calls.append(call)
        return self.response


def _drain(gen) -> List[Dict[str, Any]]:
    """Drain a Strands async stream into a list for easy assertions."""

    async def _run() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        async for event in gen:
            out.append(event)
        return out

    return asyncio.run(_run())


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


def test_flatten_system_prompt_content_handles_absence_and_non_dict_blocks() -> None:
    assert _flatten_system_prompt_content(None) == ""
    assert _flatten_system_prompt_content([]) == ""
    assert _flatten_system_prompt_content([{"text": "a"}, {"text": "b"}]) == "ab"
    assert _flatten_system_prompt_content(["already-a-string"]) == "already-a-string"


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


def test_stream_records_observer_turns_for_each_chat_call() -> None:
    """Each Strands stream() invocation is one model HTTP turn for transcripts."""
    client = _RecordingClient({"summary": "done", "status": "ok"})
    model = LLMClientModel(client, agent_key="qa_agent")

    async def _run() -> list:
        events = []
        async for event in model.stream(
            messages=[{"role": "user", "content": [{"text": "review this"}]}],
            system_prompt="You are a QA expert.",
        ):
            events.append(event)
        return take_complete_json_turns()

    turns = asyncio.run(_run())
    assert len(turns) == 1
    prompt, response, _started = turns[0]
    assert json.loads(prompt)[0]["role"] == "system"
    assert "QA expert" in prompt
    assert "done" in response


def test_stream_replays_provider_turns_recorded_inside_to_thread() -> None:
    """chat() runs in a worker thread; ContextVar writes there must be
    returned and re-recorded on the awaiting task or self-correction
    turns never reach the transcript observer."""

    class _InnerTurnClient(_RecordingClient):
        def chat(self, messages: list, **kwargs: Any) -> Any:
            super().chat(messages, **kwargs)
            record_complete_json_turn("first messages", "prose analysis")
            record_complete_json_turn("corrective messages", '{"ok": true}')
            return self.response

    client = _InnerTurnClient({"ok": True})
    model = LLMClientModel(client, agent_key="qa_agent")

    async def _run() -> list:
        async for _event in model.stream(
            messages=[{"role": "user", "content": [{"text": "review this"}]}],
        ):
            pass
        return take_complete_json_turns()

    turns = asyncio.run(_run())
    assert [(p, r) for p, r, _s in turns] == [
        ("first messages", "prose analysis"),
        ("corrective messages", '{"ok": true}'),
    ]


def test_stream_merges_system_prompt_content_blocks() -> None:
    """``system_prompt_content`` (Strands' structured system prompt) must not be
    silently dropped — its flattened text reaches the emitted system message
    even when no plain ``system_prompt`` string is given."""
    client = _RecordingClient({"ok": True})
    model = LLMClientModel(client)

    _drain(
        model.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system_prompt_content=[{"text": "Follow the house style."}],
        )
    )

    call = client.chat_calls[0]
    assert call["messages"][0] == {"role": "system", "content": "Follow the house style."}


def test_stream_combines_system_prompt_and_system_prompt_content() -> None:
    """Both ``system_prompt`` and ``system_prompt_content`` may be supplied
    together — both must reach the wire, merged into one system message."""
    client = _RecordingClient({"ok": True})
    model = LLMClientModel(client)

    _drain(
        model.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system_prompt="You are a QA expert.",
            system_prompt_content=[{"text": "Follow the house style."}],
        )
    )

    call = client.chat_calls[0]
    assert call["messages"][0] == {
        "role": "system",
        "content": "You are a QA expert.\n\nFollow the house style.",
    }


def test_stream_propagates_team_through_to_thread() -> None:
    """The team bound on the calling task survives the ``to_thread`` hand-off.

    The adapter derives/binds the team before dispatching to the worker thread,
    where ``caller_team()`` could no longer see the agent frame. This proves the
    bound team reaches ``chat`` (which runs in the worker).
    """
    from llm_service.attribution import current_attribution, llm_attribution

    seen: Dict[str, Any] = {}

    class _TeamClient(_RecordingClient):
        def chat(self, messages: list, **kwargs: Any) -> Any:  # type: ignore[override]
            seen["team"] = current_attribution().team
            return super().chat(messages, **kwargs)

    model = LLMClientModel(_TeamClient({"ok": True}), agent_key="qa_agent")
    with llm_attribution(team="orchestrated"):
        _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert seen["team"] == "orchestrated"


def test_stream_binds_configured_agent_key_for_raw_client() -> None:
    """A caller-supplied (unwrapped) client is still attributed to the configured
    agent_key, since the adapter binds it before the ``to_thread`` hand-off."""
    from llm_service.attribution import current_attribution

    seen: Dict[str, Any] = {}

    class _AgentClient(_RecordingClient):
        def chat(self, messages: list, *, objective: str = "", **kwargs: Any) -> Any:  # type: ignore[override]
            seen["agent_key"] = current_attribution().agent_key
            seen["objective"] = objective
            return super().chat(messages, **kwargs)

    model = LLMClientModel(_AgentClient({"ok": True}), agent_key="foo")
    _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert seen["agent_key"] == "foo"
    # No bound objective → the generic strands objective is used as a fallback.
    assert seen["objective"] == "strands agent turn (foo)"


def test_stream_falls_back_to_derived_agent_when_unkeyed(monkeypatch) -> None:
    """An unkeyed model (``get_strands_model()`` with no agent_key) records a
    path-derived agent identity instead of an empty agent_key."""
    import llm_service.strands_adapter as adapter_mod
    from llm_service.attribution import current_attribution

    monkeypatch.setattr(adapter_mod, "caller_agent", lambda: "ui_design")
    seen: Dict[str, Any] = {}

    class _AgentClient(_RecordingClient):
        def chat(self, messages: list, *, objective: str = "", **kwargs: Any) -> Any:  # type: ignore[override]
            seen["agent_key"] = current_attribution().agent_key
            return super().chat(messages, **kwargs)

    model = LLMClientModel(_AgentClient({"ok": True}), agent_key=None)
    _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert seen["agent_key"] == "ui_design"


def test_stream_recovers_agent_key_from_wrapped_client_when_unkeyed(monkeypatch) -> None:
    """A keyed client adapted without repeating agent_key — e.g.
    ``get_strands_model(client=get_client("backend"))`` — is still attributed to
    that key. The adapter recovers it from the backing ``_AttributingClient``
    before the path-derived fallback, even though the dispatch unwraps it."""
    import llm_service.strands_adapter as adapter_mod
    from llm_service.attribution import current_attribution
    from llm_service.factory import attributed_client

    # If the path fallback were reached it would record "wrong_path"; assert it isn't.
    monkeypatch.setattr(adapter_mod, "caller_agent", lambda: "wrong_path")
    seen: Dict[str, Any] = {}

    class _AgentClient(_RecordingClient):
        def chat(self, messages: list, *, objective: str = "", **kwargs: Any) -> Any:  # type: ignore[override]
            seen["agent_key"] = current_attribution().agent_key
            return super().chat(messages, **kwargs)

    keyed = attributed_client(_AgentClient({"ok": True}), "backend")
    model = LLMClientModel(keyed, agent_key=None)
    _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert seen["agent_key"] == "backend"


def test_structured_output_recovers_agent_key_from_wrapped_client(monkeypatch) -> None:
    """``structured_output`` mirrors ``stream``: a backing ``_AttributingClient``'s
    key is recovered when the adapter itself is unkeyed."""
    import llm_service.strands_adapter as adapter_mod
    from llm_service.attribution import current_attribution
    from llm_service.factory import attributed_client

    monkeypatch.setattr(adapter_mod, "caller_agent", lambda: "wrong_path")
    seen: Dict[str, Any] = {}

    class _AgentClient(_RecordingClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
            seen["agent_key"] = current_attribution().agent_key
            return {"value": 1}

    class _Out(BaseModel):
        value: int

    keyed = attributed_client(_AgentClient({"value": 1}), "backend")
    model = LLMClientModel(keyed, agent_key=None)

    async def _run() -> None:
        async for _ in model.structured_output(
            _Out, [{"role": "user", "content": [{"text": "hi"}]}]
        ):
            pass

    asyncio.run(_run())
    assert seen["agent_key"] == "backend"


def test_stream_uses_bound_objective_over_generic() -> None:
    """A task-specific objective bound by the caller (e.g. the PA wrapper) is
    forwarded instead of the adapter's generic placeholder."""
    from llm_service.attribution import llm_attribution

    seen: Dict[str, Any] = {}

    class _ObjClient(_RecordingClient):
        def chat(self, messages: list, *, objective: str = "", **kwargs: Any) -> Any:  # type: ignore[override]
            seen["objective"] = objective
            return super().chat(messages, **kwargs)

    model = LLMClientModel(_ObjClient({"ok": True}), agent_key="foo")
    with llm_attribution(objective="classify user intent"):
        _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert seen["objective"] == "classify user intent"


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
# response_format routing
# ---------------------------------------------------------------------------


def test_stream_defaults_to_json_response_format_for_backward_compat() -> None:
    """The default model config must forward ``response_format="json"`` to
    ``LLMClient.chat`` so the backing client forces ``response_format=json_object``
    on the wire.

    Regression: a previous iteration of this adapter defaulted to text mode
    (no ``response_format=json_object`` on the wire), which broke Strands
    agents that ask for JSON in their system prompt and then ``json.loads``
    the assistant content (e.g. ``RoutePlannerAgent``). The default must
    keep the JSON path so those agents continue to receive well-formed JSON.
    """
    client = _RecordingClient({"ordered_stops": [], "route_summary": "ok"})
    model = LLMClientModel(client)
    assert model.get_config()["response_format"] == "json"

    _drain(model.stream(messages=[{"role": "user", "content": [{"text": "plan a route"}]}]))

    assert len(client.chat_calls) == 1
    assert client.chat_calls[0]["response_format"] == "json"


def test_stream_forwards_text_response_format_when_configured() -> None:
    """Opt-in ``response_format="text"`` flows through to ``LLMClient.chat``
    so the backing client uses the prose path (no JSON forcing on the wire)."""
    client = _RecordingClient("Hi there — happy to help.")
    model = LLMClientModel(client, response_format="text")

    events = _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hello"}]}]))

    assert len(client.chat_calls) == 1
    assert client.chat_calls[0]["response_format"] == "text"
    # The prose string is emitted as-is (no JSON serialization wrap).
    text_event = next(e for e in events if "contentBlockDelta" in e)
    assert text_event["contentBlockDelta"]["delta"]["text"] == "Hi there — happy to help."


def test_invocation_state_can_override_response_format_per_call() -> None:
    """Per-call ``response_format`` in ``invocation_state`` overrides the default."""
    client = _RecordingClient("prose")
    model = LLMClientModel(client)  # default json

    _drain(
        model.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            invocation_state={"response_format": "text"},
        )
    )

    assert len(client.chat_calls) == 1
    assert client.chat_calls[0]["response_format"] == "text"


def test_llm_client_model_rejects_invalid_response_format() -> None:
    """Invalid ``response_format`` values fail fast at construction time."""
    with pytest.raises(ValueError, match="response_format"):
        LLMClientModel(_RecordingClient({}), response_format="xml")


def test_llm_client_model_rejects_none_client() -> None:
    """A ``None`` client must fail fast at construction with a clear error
    instead of surfacing as a confusing ``AttributeError`` on first use."""
    with pytest.raises(ValueError, match="client is required"):
        LLMClientModel(client=None)


def test_get_strands_model_forwards_response_format() -> None:
    client = _RecordingClient({"ok": True})
    model = get_strands_model(client=client, response_format="text")
    assert model.get_config()["response_format"] == "text"


def test_get_strands_model_does_not_alias_distinct_agent_keys(monkeypatch) -> None:
    """Two agents that resolve to the same model get distinct cached adapters,
    each carrying its own agent_key — so the shared cache can't attribute one
    agent's calls to another."""
    from llm_service import provider_store as ps
    from llm_service.strands_provider import (
        _clear_strands_model_cache_for_testing,
        get_strands_model,
    )

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "shared-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434")
    # The provider list is the sole source of LLM resolution — seed a local Ollama
    # entry so get_client resolves (the shared model resolver still drives model_id).
    _entry = ps.ProviderEntry(
        id=1,
        label="e",
        provider="ollama",
        model="",
        base_url="http://localhost:11434",
        api_key="",
        sort_order=1,
        limit_exceeded=False,
        limit_type="",
        reset_at=None,
    )
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [_entry])
    monkeypatch.setattr(ps, "select_active_entry", lambda es, **k: es[0])
    _clear_strands_model_cache_for_testing()
    try:
        m_a = get_strands_model("agent_a")
        m_b = get_strands_model("agent_b")
        assert m_a is not m_b
        assert m_a._config.agent_key == "agent_a"
        assert m_b._config.agent_key == "agent_b"
        # Same key still hits the cache (identity preserved per key).
        assert get_strands_model("agent_a") is m_a
    finally:
        _clear_strands_model_cache_for_testing()


def test_clone_returns_new_model_sharing_backing_client() -> None:
    """``LLMClientModel.clone(response_format=...)`` derives a sibling that
    reuses the same backing client but applies the override. Use case:
    callers that need both prose and JSON variants of the same upstream
    model (e.g. ``BlogWriterAgent`` for the ``---DRAFT---`` marker path
    plus its JSON helpers).
    """
    client = _RecordingClient("hello")
    base = LLMClientModel(
        client,
        agent_key="blog",
        temperature=0.3,
        think=True,
        response_format="json",
    )
    sibling = base.clone(response_format="text")

    assert sibling is not base
    assert sibling._client is base._client
    assert sibling.get_config()["response_format"] == "text"
    # Non-overridden fields inherited.
    assert sibling.get_config()["agent_key"] == "blog"
    assert sibling.get_config()["temperature"] == 0.3
    assert sibling.get_config()["think"] is True
    # Base config is unmodified.
    assert base.get_config()["response_format"] == "json"

    # Verify the cloned text-mode sibling forwards response_format="text".
    _drain(sibling.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert len(client.chat_calls) == 1
    assert client.chat_calls[0]["response_format"] == "text"


def test_client_property_exposes_backing_client() -> None:
    """The public ``client`` property returns the backing LLMClient without reaching
    into the private ``_client`` attribute."""
    client = _RecordingClient("hi")
    model = LLMClientModel(client, response_format="json")
    assert model.client is client


def test_clone_rejects_invalid_response_format() -> None:
    base = LLMClientModel(_RecordingClient({}))
    with pytest.raises(ValueError, match="response_format"):
        base.clone(response_format="xml")


def test_public_llm_service_get_strands_model_accepts_response_format(monkeypatch) -> None:
    """Regression: ``llm_service.get_strands_model`` is re-exported from
    ``strands_provider`` (not ``strands_adapter``). The branding assistant calls
    it as ``get_strands_model("branding_assistant", response_format="text")``,
    so the provider's signature must accept that keyword or production init
    raises ``TypeError`` before serving any request.
    """
    from llm_service import factory
    from llm_service import get_strands_model as public_get_strands_model
    from llm_service.strands_provider import _clear_strands_model_cache_for_testing

    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    factory.clear_client_cache()
    _clear_strands_model_cache_for_testing()
    try:
        model_text = public_get_strands_model("test_agent", response_format="text")
        model_json = public_get_strands_model("test_agent", response_format="json")
    finally:
        _clear_strands_model_cache_for_testing()

    assert isinstance(model_text, LLMClientModel)
    assert model_text.get_config()["response_format"] == "text"
    assert model_json.get_config()["response_format"] == "json"
    # Distinct response_format values must not collide in the cache.
    assert model_text is not model_json


def test_public_llm_service_get_strands_model_accepts_client_kwarg() -> None:
    """Regression: the SE v2 phases' ``_resolve_model`` helpers call
    ``get_strands_model(client=llm, response_format="text")`` to wrap an
    injected ``LLMClient`` instance. The public re-export must accept the
    ``client=`` kwarg — otherwise every backend_code_v2 / frontend_code_v2
    planning, execution, review, and problem-solving call raises ``TypeError``
    the moment an ``LLMClient`` (e.g. ``OllamaLLMClient`` in production) is
    handed in.

    Tests don't catch this because ``DummyLLMClient`` also implements the
    Strands ``Model`` interface, so ``_resolve_model`` short-circuits on
    ``isinstance(llm, _StrandsModel)`` before reaching the ``client=`` branch.
    """
    from llm_service import get_strands_model as public_get_strands_model

    client = DummyLLMClient()
    model = public_get_strands_model("test_agent", client=client, response_format="text")
    assert isinstance(model, LLMClientModel)
    assert model._client is client
    assert model.get_config()["response_format"] == "text"


def test_adapter_get_strands_model_is_not_publicly_exported() -> None:
    """``strands_adapter`` must not export a public ``get_strands_model`` name:
    ``llm_service.get_strands_model`` (re-exported from ``strands_provider``,
    which adds caching, provider resolution, and fingerprint invalidation) is
    the sole canonical public entry point. The adapter's own factory is a
    low-level, package-private helper (``_get_strands_model``).
    """
    import llm_service.strands_adapter as adapter

    assert "get_strands_model" not in adapter.__all__
    assert not hasattr(adapter, "get_strands_model")
    assert hasattr(adapter, "_get_strands_model")


def test_invocation_state_invalid_response_format_raises() -> None:
    """Per-call ``response_format`` overrides via ``invocation_state`` must
    match the same strictness as ``__init__`` and ``clone``. Silently
    routing a typo (``"Text"``, ``"prose"``) to JSON mode is a worse failure
    than crashing visibly.
    """
    client = _RecordingClient("hello")
    model = LLMClientModel(client)

    async def _run() -> None:
        async for _ in model.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            invocation_state={"response_format": "prose"},
        ):
            pass

    with pytest.raises(ValueError, match="response_format"):
        asyncio.run(_run())


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
    # The output_model class itself is forwarded so a class-identity-routing
    # client (e.g. the dummy stub) doesn't have to infer it from prompt text.
    assert client.complete_json_calls[0]["structured_output_model"] is _Review


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


def test_llm_client_config_validates_response_format_at_construction() -> None:
    """The frozen dataclass enforces the contract in one place — invalid
    response_format values raise from ``__post_init__`` so they can never
    propagate to ``stream()``.
    """
    from llm_service.strands_adapter import LLMClientConfig

    with pytest.raises(ValueError, match="response_format"):
        LLMClientConfig(response_format="xml")  # type: ignore[arg-type]


def test_update_config_rejects_unknown_keys() -> None:
    """The previous mutable-dict ``self.config.update(...)`` silently retained
    typo'd or unknown keys. The dataclass-backed ``update_config`` raises
    ``TypeError`` instead, which is what we want — unknown keys never had a
    meaningful effect and silently retaining them just hid bugs.
    """
    model = LLMClientModel(DummyLLMClient())
    with pytest.raises(TypeError):
        model.update_config(this_is_not_a_known_field="oops")


def test_update_config_validates_response_format() -> None:
    """``update_config`` reuses the dataclass validation — typo'd values raise."""
    model = LLMClientModel(DummyLLMClient())
    with pytest.raises(ValueError, match="response_format"):
        model.update_config(response_format="Prose")


def test_get_config_returns_independent_dict_view() -> None:
    """Mutations on the dict returned by ``get_config()`` must not affect the
    frozen underlying config — this was a real footgun with the previous
    ``self.config: Dict`` design where the returned dict WAS the live config."""
    model = LLMClientModel(DummyLLMClient(), agent_key="qa")
    snapshot = model.get_config()
    snapshot["agent_key"] = "mutated"

    fresh = model.get_config()
    assert fresh["agent_key"] == "qa"


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
    factory.clear_client_cache()

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
        def chat(self, *a: Any, **kw: Any) -> Any:  # type: ignore[override]
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


# ---------------------------------------------------------------------------
# get_max_context_tokens delegation
# ---------------------------------------------------------------------------


class _ContextClient(DummyLLMClient):
    def get_max_context_tokens(self) -> int:
        return 262144


def test_adapter_delegates_get_max_context_tokens() -> None:
    """context_sizing helpers receive the adapter where an LLMClient is
    expected (e.g. quality_gate_tools.run_code_review); the adapter must
    answer for its backing client instead of raising AttributeError."""
    model = LLMClientModel(_ContextClient())
    assert model.get_max_context_tokens() == 262144


def test_get_strands_model_exposes_context_for_hasattr_guards() -> None:
    """llm_service.compaction guards with hasattr and silently degraded the
    adapter to a 16384-token assumption; delegation makes the guard pass."""
    model = get_strands_model(client=_ContextClient())
    assert hasattr(model, "get_max_context_tokens")
    assert model.get_max_context_tokens() == 262144


# ---------------------------------------------------------------------------
# supports_structured_output delegation
# ---------------------------------------------------------------------------


class _StructuredOutputClient(DummyLLMClient):
    def supports_structured_output(self) -> bool:
        return True


def test_llm_client_model_supports_structured_output_delegates_to_backing_client() -> None:
    """The adapter must answer for its backing client's capability flag, matching the
    get_max_context_tokens delegation precedent above."""
    assert LLMClientModel(_StructuredOutputClient()).supports_structured_output() is True
    assert LLMClientModel(DummyLLMClient()).supports_structured_output() is False


# ---------------------------------------------------------------------------
# Thinking levels through the adapter
# ---------------------------------------------------------------------------


def test_adapter_passes_think_level_string_through() -> None:
    """A level string configured on the model must reach the client verbatim —
    a bool() coercion would silently turn "medium" into True."""
    client = _RecordingClient({"summary": "done"})
    model = LLMClientModel(client, think="medium")
    _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert client.chat_calls[0]["think"] == "medium"


def test_adapter_default_think_is_none_for_client_side_resolution() -> None:
    """None defers to the client, which resolves the platform default
    (max thinking level for registered models)."""
    client = _RecordingClient({"summary": "done"})
    model = LLMClientModel(client)
    _drain(model.stream(messages=[{"role": "user", "content": [{"text": "hi"}]}]))
    assert client.chat_calls[0]["think"] is None


# ---------------------------------------------------------------------------
# Reasoning round-trip on tool-call turns
# ---------------------------------------------------------------------------


def test_stream_emits_reasoning_block_before_tool_use() -> None:
    """The envelope's reasoning must surface as a strands reasoningContent
    block so the Agent's history carries it back on the next turn."""
    client = _RecordingClient(
        {
            "__tool_calls__": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "git_status", "arguments": "{}"},
                }
            ],
            "__reasoning_content__": "thinking...",
        }
    )
    model = LLMClientModel(client)
    events = _drain(model.stream(messages=[{"role": "user", "content": [{"text": "go"}]}]))
    reasoning_deltas = [
        e["contentBlockDelta"]["delta"]["reasoningContent"]["text"]
        for e in events
        if "contentBlockDelta" in e and "reasoningContent" in e["contentBlockDelta"]["delta"]
    ]
    assert reasoning_deltas == ["thinking..."]
    assert events[-1] == {"messageStop": {"stopReason": "tool_use"}}


def test_messages_to_openai_maps_reasoning_block_onto_tool_call_message() -> None:
    """DeepSeek requires the tool-call turn's reasoning_content on the wire
    when the history is replayed; reasoningContent blocks must not be dropped."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "thought hard"}}},
                {
                    "toolUse": {
                        "toolUseId": "c1",
                        "name": "git_status",
                        "input": {},
                    }
                },
            ],
        }
    ]
    out = _strands_messages_to_openai(messages)
    assistant = next(m for m in out if m["role"] == "assistant" and m.get("tool_calls"))
    # Both dialects must be present: Ollama-compat reads `reasoning`,
    # DeepSeek-native reads `reasoning_content`.
    assert assistant["reasoning"] == "thought hard"
    assert assistant["reasoning_content"] == "thought hard"
