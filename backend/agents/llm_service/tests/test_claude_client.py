"""Tests for ClaudeLLMClient — JSON/text/chat, tool calls, thinking mapping,
error mapping, and telemetry. The Anthropic SDK is faked (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

import llm_service.clients.claude as _claude_mod
from llm_service.clients.claude import ClaudeLLMClient, _to_anthropic_tools
from llm_service.interface import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStreamCtx:
    def __init__(self, message=None, exc=None):
        self._message = message
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get_final_message(self):
        if self._exc is not None:
            raise self._exc
        return self._message


class _FakeMessages:
    def __init__(self, ctx, capture):
        self._ctx = ctx
        self._capture = capture

    def stream(self, **kwargs):
        self._capture.clear()
        self._capture.update(kwargs)
        return self._ctx


class _FakeClient:
    def __init__(self, ctx, capture):
        self.messages = _FakeMessages(ctx, capture)


def _text_message(text, *, stop_reason="end_turn", input_tokens=11, output_tokens=7):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _tool_message(name, tool_input, *, tool_id="toolu_1"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )


def _make_client(message=None, exc=None, *, model="claude-opus-4-8"):
    """Return (client, capture) with the Anthropic SDK call faked."""
    capture: dict = {}
    client = ClaudeLLMClient(model=model, api_key="sk-test")
    client._client = _FakeClient(_FakeStreamCtx(message=message, exc=exc), capture)
    return client, capture


# ---------------------------------------------------------------------------
# complete_json
# ---------------------------------------------------------------------------


def test_complete_json_parses_object():
    client, _ = _make_client(_text_message('{"answer": 42}'))
    out = client.complete_json("q", objective="test")
    assert out == {"answer": 42}


def test_complete_json_repairs_fenced_json():
    client, _ = _make_client(_text_message('```json\n{"a": 1, "b": [1,2,3]}\n```'))
    out = client.complete_json("q", objective="test")
    assert out == {"a": 1, "b": [1, 2, 3]}


def test_complete_json_accepts_schema_kwarg_as_noop():
    """Claude has no decoder-level schema enforcement — schema= is silently ignored."""
    client, _ = _make_client(_text_message('{"answer": 42}'))
    assert client.supports_structured_output() is False
    out = client.complete_json("q", objective="test", schema={"type": "object"})
    assert out == {"answer": 42}


def test_claude_supports_prompt_caching_is_true():
    client, _ = _make_client(_text_message("ok"))
    assert client.supports_prompt_caching() is True


def test_complete_json_never_sends_temperature():
    client, capture = _make_client(_text_message('{"ok": true}'))
    client.complete_json("q", objective="test", temperature=0.9)
    assert "temperature" not in capture
    assert "top_p" not in capture


def test_complete_json_augments_system_prompt():
    client, capture = _make_client(_text_message('{"ok": true}'))
    client.complete_json("q", objective="test", system_prompt="Be terse.")
    assert capture["system"].startswith("Be terse.")
    assert "JSON" in capture["system"]


def test_complete_json_tool_call_envelope():
    client, _ = _make_client(_tool_message("get_weather", {"city": "Paris"}))
    out = client.complete_json("q", objective="test", tools=[{"name": "get_weather"}])
    assert "__tool_calls__" in out
    call = out["__tool_calls__"][0]
    assert call["function"]["name"] == "get_weather"
    assert call["function"]["arguments"] == {"city": "Paris"}


def test_complete_json_with_tools_omits_json_instruction():
    # With tools, the "JSON only" instruction must NOT be appended (it fights tool use).
    client, capture = _make_client(_tool_message("f", {}))
    client.complete_json("q", objective="t", system_prompt="Be terse.", tools=[{"name": "f"}])
    assert capture["system"] == "Be terse."
    assert "JSON" not in capture["system"]


def test_complete_json_with_tools_no_system_prompt_sends_no_system():
    client, capture = _make_client(_tool_message("f", {}))
    client.complete_json("q", objective="t", tools=[{"name": "f"}])
    assert "system" not in capture


def test_complete_json_requires_prompt():
    client, _ = _make_client(_text_message("{}"))
    with pytest.raises(ValueError):
        client.complete_json("   ", objective="t")


def test_complete_requires_prompt():
    client, _ = _make_client(_text_message("hi"))
    with pytest.raises(ValueError):
        client.complete("", objective="t")


def test_pause_turn_raises_truncated():
    client, _ = _make_client(_text_message("partial", stop_reason="pause_turn"))
    with pytest.raises(LLMTruncatedError):
        client.complete("q", objective="t")


def test_max_tokens_env_zero_is_treated_as_unset(monkeypatch):
    from llm_service.clients.claude import DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "0")
    client, capture = _make_client(_text_message("{}"))
    client.complete_json("q", objective="t")
    assert capture["max_tokens"] == DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS


def test_non_anthropic_error_mapped_to_permanent():
    # A client-side error (e.g. a bad kwarg an older SDK rejects) must not escape raw.
    client, _ = _make_client(exc=TypeError("unexpected keyword argument 'output_config'"))
    with pytest.raises(LLMPermanentError):
        client.complete("q", objective="t")


def test_on_reasoning_receives_thinking_deltas():
    captured: list[str] = []

    class _IterStream:
        def __init__(self, msg):
            self._msg = msg

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def __iter__(self):
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking="step1 "),
            )
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="ignored"),
            )
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking="step2"),
            )

        def get_final_message(self):
            return self._msg

    class _Msgs:
        def stream(self, **_kw):
            return _IterStream(_text_message("done"))

    client = ClaudeLLMClient(model="claude-opus-4-8", api_key="sk", on_reasoning=captured.append)
    client._client = SimpleNamespace(messages=_Msgs())
    assert client.complete("q", objective="t") == "done"
    assert "".join(captured) == "step1 step2"


def test_chat_json_parse_error_records_prompt_and_response(monkeypatch):
    import llm_service.clients.claude as mod
    from llm_service.interface import LLMJsonParseError

    records: list[dict] = []
    monkeypatch.setattr(mod, "record_llm_call", lambda **kw: records.append(kw))
    client, _ = _make_client(_text_message("not json"))
    with pytest.raises(LLMJsonParseError):
        client.chat([{"role": "user", "content": "q"}], objective="t", response_format="json")
    errors = [r for r in records if r.get("status") == "error"]
    assert errors and errors[0]["response_text"] == "not json"
    assert errors[0]["prompt_text"]  # non-empty serialized messages


def test_to_anthropic_messages_skips_empty_user_content():
    from llm_service.clients.claude import _to_anthropic_messages

    _system, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": ""},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "   "},
        ]
    )
    assert msgs == [{"role": "user", "content": "hello"}]


def test_to_anthropic_messages_drops_empty_id_tool_result():
    from llm_service.clients.claude import _to_anthropic_messages

    _system, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "", "content": "result"},
        ]
    )
    # The orphan tool_result with an empty id must not be emitted to Anthropic.
    has_tool_result = any(
        isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"])
        for m in msgs
    )
    assert not has_tool_result


def test_to_anthropic_messages_system_string_unchanged_without_breakpoint():
    """No CacheBreakpoint anywhere -> identical str-typed system, unaffected by
    the caching feature (regression guard for the pre-caching behavior)."""
    from llm_service.clients.claude import _to_anthropic_messages

    system, _msgs = _to_anthropic_messages(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "You are terse."
    assert isinstance(system, str)


def test_to_anthropic_messages_translates_cache_breakpoint_to_cache_control_block():
    from llm_service.cache_breakpoint import CacheBreakpoint
    from llm_service.clients.claude import _to_anthropic_messages

    system, _msgs = _to_anthropic_messages(
        [
            {
                "role": "system",
                "content": [CacheBreakpoint("stable prefix"), "\n\ntrailer text"],
            },
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == [
        {"type": "text", "text": "stable prefix", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\ntrailer text"},
    ]


def test_to_anthropic_messages_list_system_without_breakpoint_still_flattens_to_str():
    """A list-typed system content with no CacheBreakpoint still renders as a
    plain joined string (the str/list branch point is CacheBreakpoint presence,
    not content shape)."""
    from llm_service.clients.claude import _to_anthropic_messages

    system, _msgs = _to_anthropic_messages(
        [
            {"role": "system", "content": ["lead", "trail"]},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "lead\n\ntrail"


def test_json_system_appends_instruction_to_block_list_system():
    from llm_service.clients.claude import _JSON_ONLY_INSTRUCTION, _json_system

    blocks = [{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}]
    result = _json_system(blocks, tools=None)
    assert result == [*blocks, {"type": "text", "text": _JSON_ONLY_INSTRUCTION}]


def test_json_system_leaves_block_list_system_unchanged_when_tools_present():
    from llm_service.clients.claude import _json_system

    blocks = [{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}]
    assert _json_system(blocks, tools=[{"name": "f"}]) == blocks


def test_chat_wire_payload_carries_cache_control_for_breakpoint_system():
    from llm_service.cache_breakpoint import CacheBreakpoint

    client, capture = _make_client(_text_message("hi there"))
    client.chat(
        [
            {"role": "system", "content": [CacheBreakpoint("stable prefix"), "more"]},
            {"role": "user", "content": "hi"},
        ],
        objective="test",
        response_format="text",
    )
    assert capture["system"][0] == {
        "type": "text",
        "text": "stable prefix",
        "cache_control": {"type": "ephemeral"},
    }
    assert capture["system"][1] == {"type": "text", "text": "more"}


def test_complete_json_requires_objective():
    client, _ = _make_client(_text_message("{}"))
    with pytest.raises(ValueError):
        client.complete_json("q", objective="  ")


def test_complete_json_json_parse_error_propagates():
    from llm_service.interface import LLMJsonParseError

    client, _ = _make_client(_text_message("this is not json at all"))
    with pytest.raises(LLMJsonParseError):
        client.complete_json("q", objective="test")


# ---------------------------------------------------------------------------
# thinking / effort
# ---------------------------------------------------------------------------


def test_thinking_default_is_off_for_json_mode(monkeypatch):
    """complete_json is always JSON mode; with no explicit think and no agent
    pin, extended thinking competes with strict JSON decoding for the content
    channel, so the default omits the thinking kwarg entirely."""
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    client, capture = _make_client(_text_message("{}"))
    client.complete_json("q", objective="t")
    assert "thinking" not in capture
    assert "output_config" not in capture


def test_thinking_default_is_adaptive_for_text_mode(monkeypatch):
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    client, capture = _make_client(_text_message("hello"))
    client.complete("q", objective="t")
    assert capture["thinking"] == {"type": "adaptive"}
    assert "output_config" not in capture


def test_thinking_explicit_true_is_adaptive_even_in_json_mode():
    client, capture = _make_client(_text_message("{}"))
    client.complete_json("q", objective="t", think=True)
    assert capture["thinking"] == {"type": "adaptive"}
    assert "output_config" not in capture


def test_thinking_level_maps_to_effort():
    client, capture = _make_client(_text_message("{}"))
    client.complete_json("q", objective="t", think="high")
    assert capture["thinking"] == {"type": "adaptive"}
    assert capture["output_config"] == {"effort": "high"}


def test_thinking_disabled_omits_thinking():
    client, capture = _make_client(_text_message("{}"))
    client.complete_json("q", objective="t", think=False)
    assert "thinking" not in capture
    assert "output_config" not in capture


# ---------------------------------------------------------------------------
# complete / chat
# ---------------------------------------------------------------------------


def test_complete_returns_text():
    client, _ = _make_client(_text_message("hello world"))
    assert client.complete("hi", objective="t") == "hello world"


def test_complete_tool_call_returns_json_string():
    import json

    client, _ = _make_client(_tool_message("do_it", {"x": 1}))
    out = client.complete("hi", objective="t", tools=[{"name": "do_it"}])
    parsed = json.loads(out)
    assert parsed["__tool_calls__"][0]["function"]["name"] == "do_it"


def test_chat_json_mode_parses():
    client, _ = _make_client(_text_message('{"v": 1}'))
    out = client.chat([{"role": "user", "content": "hi"}], objective="t")
    assert out == {"v": 1}


def test_chat_text_mode_returns_raw():
    client, _ = _make_client(_text_message("prose response"))
    out = client.chat([{"role": "user", "content": "hi"}], objective="t", response_format="text")
    assert out == "prose response"


def test_chat_splits_system_message():
    client, capture = _make_client(_text_message('{"ok": 1}'))
    client.chat(
        [
            {"role": "system", "content": "You are X."},
            {"role": "user", "content": "hi"},
        ],
        objective="t",
    )
    assert "You are X." in capture["system"]
    assert capture["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_rejects_bad_response_format():
    client, _ = _make_client(_text_message("{}"))
    with pytest.raises(ValueError):
        client.chat([{"role": "user", "content": "hi"}], objective="t", response_format="xml")


# ---------------------------------------------------------------------------
# tool-loop message translation (_to_anthropic_messages)
# ---------------------------------------------------------------------------


def test_to_anthropic_messages_translates_tool_loop_sequence():
    from llm_service.clients.claude import _to_anthropic_messages

    system, msgs = _to_anthropic_messages(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": '{"x": 1}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": '{"ok": true}'},
        ]
    )
    assert system == "be brief"
    # assistant turn carries a tool_use block; arguments string parsed to dict
    assert msgs[0] == {"role": "user", "content": "do it"}
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"x": 1}}
    ]
    # tool result becomes a tool_result block in a user turn (never dropped)
    assert msgs[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok": true}'}],
    }


def test_to_anthropic_messages_coalesces_multiple_tool_results():
    from llm_service.clients.claude import _to_anthropic_messages

    _system, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "function": {"name": "f", "arguments": {}}},
                    {"id": "b", "function": {"name": "g", "arguments": {}}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ra"},
            {"role": "tool", "tool_call_id": "b", "content": "rb"},
        ]
    )
    # both results coalesced into ONE user turn
    results = msgs[-1]
    assert results["role"] == "user"
    assert [c["tool_use_id"] for c in results["content"]] == ["a", "b"]


def test_chat_tool_loop_messages_reach_invoke(monkeypatch):
    # End-to-end: chat() with tool-loop-style messages produces Anthropic-shaped
    # messages on the wire (the bug was these being silently dropped).
    client, capture = _make_client(_text_message('{"done": true}'))
    client.chat(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": {}}}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
        ],
        objective="t",
    )
    roles = [m["role"] for m in capture["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert capture["messages"][-1]["content"][0]["type"] == "tool_result"


def test_to_anthropic_messages_drops_orphan_tool_result():
    # An assistant turn whose tool_calls are all non-dict yields no tool_use, so
    # the following tool_result is an orphan and must be dropped (not left dangling
    # as an Anthropic-invalid tool_result with no preceding tool_use).
    from llm_service.clients.claude import _to_anthropic_messages

    _system, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": ["not-a-dict"]},
            {"role": "tool", "tool_call_id": "x", "content": "r"},
        ]
    )
    # Only the user turn survives; no dangling tool_result user turn.
    assert msgs == [{"role": "user", "content": "go"}]


def test_to_anthropic_messages_skips_empty_assistant_content():
    # A plain assistant turn with empty/whitespace content is dropped — Anthropic
    # rejects an empty assistant text block.
    from llm_service.clients.claude import _to_anthropic_messages

    _system, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "   "},
            {"role": "assistant", "content": "real reply"},
        ]
    )
    assert msgs == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "real reply"},
    ]


def test_chat_raises_when_no_messages_after_translation():
    # System-only input has nothing to send; chat surfaces a clear error rather
    # than letting an opaque Anthropic 400 escape.
    client, _ = _make_client(_text_message("{}"))
    with pytest.raises(LLMPermanentError):
        client.chat([{"role": "system", "content": "be brief"}], objective="t")


# ---------------------------------------------------------------------------
# stop reasons
# ---------------------------------------------------------------------------


def test_refusal_raises_permanent():
    client, _ = _make_client(_text_message("", stop_reason="refusal"))
    with pytest.raises(LLMPermanentError):
        client.complete("hi", objective="t")


def test_max_tokens_with_text_raises_truncated():
    client, _ = _make_client(_text_message("partial...", stop_reason="max_tokens"))
    with pytest.raises(LLMTruncatedError) as ei:
        client.complete("hi", objective="t")
    assert "partial" in str(ei.value)


def test_max_tokens_empty_output_raises_truncated_with_diagnostic():
    # Truncation that produced no text (commonly thinking-token exhaustion) still
    # raises, but the message says so rather than carrying a silent empty string.
    client, _ = _make_client(_text_message("", stop_reason="max_tokens"))
    with pytest.raises(LLMTruncatedError) as ei:
        client.complete("hi", objective="t")
    assert "no output" in str(ei.value).lower()


def test_truncated_tool_call_raises_before_envelope():
    # A tool_use block under stop_reason=max_tokens (possibly incomplete args)
    # must surface as truncation, not a "successful" tool invocation.
    msg = _tool_message("do_it", {"partial": True})
    msg.stop_reason = "max_tokens"
    client, _ = _make_client(msg)
    with pytest.raises(LLMTruncatedError):
        client.complete_json("q", objective="t", tools=[{"name": "do_it", "input_schema": {}}])


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def _http_error(cls, status, *, headers=None):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return cls("boom", response=resp, body=None)


def test_rate_limit_maps_and_parses_retry_after():
    err = _http_error(anthropic.RateLimitError, 429, headers={"retry-after": "30"})
    client, _ = _make_client(exc=err)
    with pytest.raises(LLMRateLimitError) as ei:
        client.complete("hi", objective="t")
    assert ei.value.retry_after_seconds == 30.0


def test_retry_after_not_honored_when_disabled(monkeypatch):
    # The LLM_RATE_LIMIT_HONOR_RETRY_AFTER kill-switch must apply to Claude too
    # (parity with the Ollama client): when disabled, a parsed Retry-After is
    # dropped so only the computed backoff schedule governs the wait.
    monkeypatch.setenv("LLM_RATE_LIMIT_HONOR_RETRY_AFTER", "false")
    err = _http_error(anthropic.RateLimitError, 429, headers={"retry-after": "30"})
    client, _ = _make_client(exc=err)
    with pytest.raises(LLMRateLimitError) as ei:
        client.complete("hi", objective="t")
    assert ei.value.retry_after_seconds is None


def test_server_error_maps_temporary():
    err = _http_error(anthropic.InternalServerError, 500)
    client, _ = _make_client(exc=err)
    with pytest.raises(LLMTemporaryError):
        client.complete("hi", objective="t")


def test_client_error_maps_permanent():
    err = _http_error(anthropic.BadRequestError, 400)
    client, _ = _make_client(exc=err)
    with pytest.raises(LLMPermanentError):
        client.complete("hi", objective="t")


def test_connection_error_maps_temporary():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.APIConnectionError(message="conn", request=req)
    client, _ = _make_client(exc=err)
    with pytest.raises(LLMTemporaryError):
        client.complete("hi", objective="t")


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_get_max_context_tokens_known_model():
    client, _ = _make_client(_text_message("{}"), model="claude-opus-4-8")
    assert client.get_max_context_tokens() == 1_000_000


def test_missing_api_key_raises_permanent():
    client = ClaudeLLMClient(model="claude-opus-4-8", api_key="")
    with pytest.raises(LLMPermanentError):
        client.complete("hi", objective="t")


def test_resolve_max_tokens_caps():
    client, _ = _make_client(_text_message("{}"))
    assert client._resolve_max_tokens(999_999) == 128_000
    assert client._resolve_max_tokens(None) == 32_768
    assert client._resolve_max_tokens(100) == 100
    # A non-positive explicit value is "unset" -> falls through to the default,
    # not coerced to a 1-token cap.
    assert client._resolve_max_tokens(0) == 32_768
    assert client._resolve_max_tokens(-5) == 32_768


def test_base_anthropic_error_maps_permanent():
    client, _ = _make_client(exc=anthropic.AnthropicError("internal sdk failure"))
    with pytest.raises(LLMPermanentError):
        client.complete("hi", objective="t")


def test_telemetry_recorded_with_tokens():
    from llm_service import telemetry

    telemetry.clear_call_log()
    client, _ = _make_client(_text_message('{"ok": 1}', input_tokens=12, output_tokens=8))
    client.complete_json("q", objective="record me")
    calls = telemetry.get_recent_calls()
    assert calls, "expected a telemetry record"
    rec = calls[-1]
    assert rec["model"] == "claude-opus-4-8"
    assert rec["prompt_tokens"] == 12
    assert rec["completion_tokens"] == 8
    assert rec["total_tokens"] == 20
    assert rec["status"] == "success"


def test_complete_requires_objective():
    client, _ = _make_client(_text_message("x"))
    with pytest.raises(ValueError):
        client.complete("q", objective="")


def test_chat_requires_objective():
    client, _ = _make_client(_text_message("{}"))
    with pytest.raises(ValueError):
        client.chat([{"role": "user", "content": "hi"}], objective="")


def test_tools_kwarg_forwarded_with_valid_shape():
    client, capture = _make_client(_text_message('{"ok": 1}'))
    client.complete_json(
        "q",
        objective="t",
        tools=[{"name": "f", "input_schema": {"type": "object"}}],
    )
    assert capture["tools"] == [
        {"name": "f", "description": "", "input_schema": {"type": "object"}}
    ]


def test_to_anthropic_tools_skips_non_dict():
    from llm_service.clients.claude import _to_anthropic_tools as t

    assert t([None, 5, {"name": "g", "input_schema": {}}]) == [
        {"name": "g", "description": "", "input_schema": {}}
    ]


def test_get_client_builds_and_caches_real_sdk_client():
    client = ClaudeLLMClient(model="claude-opus-4-8", api_key="sk-real")
    built = client._get_client()
    assert isinstance(built, anthropic.Anthropic)
    assert client._get_client() is built  # cached


def test_plain_apistatuserror_429_maps_rate_limit():
    err = _http_error(anthropic.APIStatusError, 429, headers={"retry-after": "12"})
    client, _ = _make_client(exc=err)
    with pytest.raises(LLMRateLimitError) as ei:
        client.complete("hi", objective="t")
    assert ei.value.retry_after_seconds == 12.0


def test_chat_tool_call_envelope():
    client, _ = _make_client(_tool_message("act", {"k": 1}))
    out = client.chat(
        [{"role": "user", "content": "hi"}],
        objective="t",
        tools=[{"name": "act", "input_schema": {}}],
    )
    assert out["__tool_calls__"][0]["function"]["name"] == "act"


def test_chat_json_parse_error_raises():
    from llm_service.interface import LLMJsonParseError

    client, _ = _make_client(_text_message("not json"))
    with pytest.raises(LLMJsonParseError):
        client.chat([{"role": "user", "content": "hi"}], objective="t")


def test_chat_splits_assistant_and_ignores_non_dict():
    client, capture = _make_client(_text_message("ok", stop_reason="end_turn"))
    client.chat(
        [
            "garbage-non-dict",
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": "now"},
        ],
        objective="t",
        response_format="text",
    )
    assert capture["messages"] == [
        {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "now"},
    ]


def test_retry_after_seconds_variants():
    from llm_service.clients.claude import _retry_after_seconds as r

    assert r(SimpleNamespace(response=None)) is None  # no response
    assert r(SimpleNamespace(response=SimpleNamespace(headers=object()))) is None  # no .get
    assert r(SimpleNamespace(response=SimpleNamespace(headers={}))) is None  # missing header
    assert r(SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "x"}))) is None  # bad
    assert (
        r(SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "0"}))) is None
    )  # non-pos
    assert r(SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "9"}))) == 9.0


def test_to_anthropic_tools_translates_both_shapes():
    openai_shape = [
        {
            "type": "function",
            "function": {"name": "f", "description": "d", "parameters": {"type": "object"}},
        }
    ]
    out = _to_anthropic_tools(openai_shape)
    assert out == [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]

    anthropic_shape = [{"name": "g", "description": "d2", "input_schema": {"type": "object"}}]
    assert _to_anthropic_tools(anthropic_shape) == anthropic_shape
    assert _to_anthropic_tools(None) is None
    assert _to_anthropic_tools([]) is None


# ---------------------------------------------------------------------------
# Slow rate-limit retry: a 429 that survives the Anthropic SDK's built-in
# retries is retried on the shared LLM_RATE_LIMIT_* schedule, mirroring Ollama.
# ---------------------------------------------------------------------------


def _invoke_kwargs():
    return dict(
        system=None,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        think=None,
        max_tokens=16,
    )


@pytest.fixture
def _fast_rate_limit(monkeypatch):
    """Tiny, deterministic 429 schedule with no real sleeping; records waits."""
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "0.01")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "0.02")
    waits: list[float] = []
    monkeypatch.setattr(_claude_mod.time, "sleep", lambda s: waits.append(s))
    return waits


def test_rate_limit_retried_then_succeeds(monkeypatch, _fast_rate_limit):
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "3")
    client = ClaudeLLMClient(model="claude-x", api_key="sk-test")
    calls = {"n": 0}
    sentinel = object()

    def fake_invoke(**_kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise LLMRateLimitError("429", status_code=429, retry_after_seconds=None)
        return sentinel

    monkeypatch.setattr(client, "_invoke", fake_invoke)
    assert client._invoke_with_rate_limit_retry(**_invoke_kwargs()) is sentinel
    assert calls["n"] == 3  # 2 failures + 1 success
    assert len(_fast_rate_limit) == 2  # slept once before each retry


def test_rate_limit_exhausts_and_reraises(monkeypatch, _fast_rate_limit):
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "2")
    client = ClaudeLLMClient(model="claude-x", api_key="sk-test")
    calls = {"n": 0}

    def always_429(**_kw):
        calls["n"] += 1
        raise LLMRateLimitError("429", status_code=429, retry_after_seconds=None)

    monkeypatch.setattr(client, "_invoke", always_429)
    with pytest.raises(LLMRateLimitError):
        client._invoke_with_rate_limit_retry(**_invoke_kwargs())
    assert calls["n"] == 3  # initial attempt + 2 retries
    assert len(_fast_rate_limit) == 2


def test_rate_limit_no_retry_when_disabled(monkeypatch, _fast_rate_limit):
    # LLM_RATE_LIMIT_MAX_RETRIES=0 -> single attempt, no sleep, immediate raise.
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "0")
    client = ClaudeLLMClient(model="claude-x", api_key="sk-test")
    calls = {"n": 0}

    def always_429(**_kw):
        calls["n"] += 1
        raise LLMRateLimitError("429", status_code=429)

    monkeypatch.setattr(client, "_invoke", always_429)
    with pytest.raises(LLMRateLimitError):
        client._invoke_with_rate_limit_retry(**_invoke_kwargs())
    assert calls["n"] == 1
    assert _fast_rate_limit == []


def test_rate_limit_honors_retry_after(monkeypatch, _fast_rate_limit):
    # A provider Retry-After raises the wait to at least that value (within cap).
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "0.01")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "100")
    client = ClaudeLLMClient(model="claude-x", api_key="sk-test")
    calls = {"n": 0}
    sentinel = object()

    def fake_invoke(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMRateLimitError("429", status_code=429, retry_after_seconds=42.0)
        return sentinel

    monkeypatch.setattr(client, "_invoke", fake_invoke)
    assert client._invoke_with_rate_limit_retry(**_invoke_kwargs()) is sentinel
    assert _fast_rate_limit and _fast_rate_limit[0] >= 42.0


def test_non_rate_limit_error_not_retried(monkeypatch, _fast_rate_limit):
    # Only 429s are retried here; other LLM errors propagate immediately.
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "3")
    client = ClaudeLLMClient(model="claude-x", api_key="sk-test")
    calls = {"n": 0}

    def boom(**_kw):
        calls["n"] += 1
        raise LLMTemporaryError("5xx", status_code=503)

    monkeypatch.setattr(client, "_invoke", boom)
    with pytest.raises(LLMTemporaryError):
        client._invoke_with_rate_limit_retry(**_invoke_kwargs())
    assert calls["n"] == 1
    assert _fast_rate_limit == []


# ---------------------------------------------------------------------------
# Global concurrency gate: _invoke holds the process-global semaphore around the
# network call only, and releases it before the 429 backoff sleep.
# ---------------------------------------------------------------------------


def test_invoke_acquires_global_concurrency_gate(monkeypatch):
    """The network exchange runs inside the shared concurrency gate: it is
    entered, the streamed message is assembled inside it, and it is released only
    afterward — so concurrent Claude calls are bounded by LLM_MAX_CONCURRENCY."""
    events: list[str] = []

    class _ProbeSem:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_a):
            events.append("exit")
            return False

    monkeypatch.setattr(_claude_mod, "get_llm_semaphore", lambda: _ProbeSem())

    class _RecordingStreamCtx(_FakeStreamCtx):
        def get_final_message(self):
            events.append("call")
            return super().get_final_message()

    capture: dict = {}
    client = ClaudeLLMClient(model="claude-opus-4-8", api_key="sk-test")
    client._client = _FakeClient(
        _RecordingStreamCtx(message=_text_message('{"ok": true}')), capture
    )

    out = client.complete_json("q", objective="t")

    assert out == {"ok": True}
    assert events == ["enter", "call", "exit"]


def test_gate_released_before_rate_limit_backoff_sleep(monkeypatch):
    """The 429 backoff sleep must never run while the concurrency slot is held,
    or a rate-limited call would keep a slot for minutes and re-create the
    concurrent-request stall. At every sleep the gate is fully released."""
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "1")
    depth = {"n": 0}
    depth_at_sleep: list[int] = []

    class _ProbeSem:
        def __enter__(self):
            depth["n"] += 1
            return self

        def __exit__(self, *_a):
            depth["n"] -= 1
            return False

    monkeypatch.setattr(_claude_mod, "get_llm_semaphore", lambda: _ProbeSem())
    monkeypatch.setattr(_claude_mod.time, "sleep", lambda _s: depth_at_sleep.append(depth["n"]))

    err = _http_error(anthropic.RateLimitError, 429)
    client, _ = _make_client(exc=err)

    with pytest.raises(LLMRateLimitError):
        client._invoke_with_rate_limit_retry(**_invoke_kwargs())

    assert depth_at_sleep == [0]  # one retry, gate released before its sleep
    assert depth["n"] == 0  # balanced — no leaked slot


# ---------------------------------------------------------------------------
# Signed thinking blocks must round-trip across tool-use turns. Under extended
# thinking (the default), Anthropic 400s if the signed thinking/redacted_thinking
# blocks from a tool-use turn are not replayed unchanged and first on the next
# request. These tests cover capture (_content_from_message), replay ordering
# (_to_anthropic_messages), and the end-to-end tool loop.
# ---------------------------------------------------------------------------


def _thinking_block(thinking="reasoning", signature="sig-abc"):
    return SimpleNamespace(type="thinking", thinking=thinking, signature=signature)


def _thinking_tool_message(name, tool_input, *, tool_id="toolu_1", signature="sig-abc"):
    return SimpleNamespace(
        content=[
            _thinking_block(signature=signature),
            SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )


def test_content_from_message_captures_signed_thinking_block():
    client, _ = _make_client()
    kind, env = client._content_from_message(
        _thinking_tool_message("get_weather", {"city": "Paris"}, signature="sig-xyz")
    )
    assert kind == "tools"
    # signature preserved verbatim alongside the tool calls
    assert env["__thinking_blocks__"] == [
        {"type": "thinking", "thinking": "reasoning", "signature": "sig-xyz"}
    ]
    assert env["__tool_calls__"][0]["function"]["name"] == "get_weather"


def test_content_from_message_captures_redacted_thinking_block():
    client, _ = _make_client()
    msg = SimpleNamespace(
        content=[
            SimpleNamespace(type="redacted_thinking", data="enc-1"),
            SimpleNamespace(type="tool_use", id="t1", name="f", input={}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    _kind, env = client._content_from_message(msg)
    assert env["__thinking_blocks__"] == [{"type": "redacted_thinking", "data": "enc-1"}]


def test_content_from_message_omits_thinking_key_when_absent():
    # A tool turn with no thinking blocks must not grow a spurious key.
    client, _ = _make_client()
    kind, env = client._content_from_message(_tool_message("f", {}))
    assert kind == "tools"
    assert "__thinking_blocks__" not in env


def test_to_anthropic_messages_replays_thinking_blocks_first():
    from llm_service.clients.claude import _to_anthropic_messages

    _system, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "thinking out loud",
                "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}],
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "reasoning", "signature": "sig-xyz"}
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "r"},
        ]
    )
    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    # Signed thinking block must be first and unchanged, then text, then tool_use.
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "reasoning",
        "signature": "sig-xyz",
    }
    assert [b["type"] for b in assistant["content"]] == ["thinking", "text", "tool_use"]


def test_to_anthropic_messages_skips_malformed_thinking_blocks():
    # Only well-formed thinking/redacted_thinking dicts are re-emitted.
    from llm_service.clients.claude import _to_anthropic_messages

    _system, msgs = _to_anthropic_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}],
                "thinking_blocks": ["not-a-dict", {"type": "text", "text": "nope"}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "r"},
        ]
    )
    assert [b["type"] for b in msgs[1]["content"]] == ["tool_use"]


def test_tool_loop_replays_signed_thinking_block():
    # End-to-end regression: a two-round Claude + tools exchange with thinking on
    # must echo the original signed thinking block, first, on the second request.
    from llm_service.tool_loop import complete_json_with_tool_loop

    sent: list[list] = []
    responses = [
        _thinking_tool_message("f", {"x": 1}, signature="sig-123"),
        _text_message('{"done": true}'),
    ]

    class _MultiMessages:
        def stream(self, **kwargs):
            sent.append(kwargs["messages"])
            return _FakeStreamCtx(message=responses[len(sent) - 1])

    client = ClaudeLLMClient(model="claude-opus-4-8", api_key="sk-test")
    client._client = SimpleNamespace(messages=_MultiMessages())

    out = complete_json_with_tool_loop(
        client,
        objective="t",
        user_prompt="go",
        system_prompt="be brief",
        tools=[{"name": "f", "input_schema": {}}],
        tool_handlers={"f": lambda _args: {"ok": True}},
        think=True,
    )
    assert out == {"done": True}
    # The second request replays the assistant tool-use turn with the signed
    # thinking block restored first (this is the 400 the issue describes).
    assistant = next(m for m in sent[1] if m["role"] == "assistant")
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "reasoning",
        "signature": "sig-123",
    }
    assert any(b["type"] == "tool_use" for b in assistant["content"])
