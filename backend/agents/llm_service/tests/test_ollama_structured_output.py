"""Tests for Ollama provider-enforced structured output (schema-forced decoding)."""

from unittest.mock import patch

import pytest
from pydantic import BaseModel

from llm_service.clients.ollama import OllamaLLMClient, _normalize_schema_for_wire
from llm_service.interface import LLMSemanticExhaustionError

from .test_ollama_client import (
    _OK_SSE,
    _REASONING_ONLY_SSE,
    _capturing_multi_client,
    _patch_no_sleep,
    _stream_cm,
)


class _Answer(BaseModel):
    selected_option_id: str
    rationale: str


def test_ollama_supports_structured_output_is_true() -> None:
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    assert client.supports_structured_output() is True


def test_ollama_complete_json_with_schema_sends_json_schema_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing schema= sends the OpenAI-compatible json_schema response_format, not json_object."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    cms = [_stream_cm(200, sse_lines=_OK_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", objective="test", temperature=0, schema=_Answer)
    assert result == {"ok": 1}
    assert captured, "No payload captured"
    payload = captured[0]
    assert "tools" not in payload
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_response",
            "schema": _Answer.model_json_schema(),
            "strict": True,
        },
    }


def test_ollama_complete_json_with_schema_and_tools_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tools and schema are mutually exclusive on the wire — fail fast before any
    chat-completions request (a prior /api/show max-tokens-resolution call is
    unrelated plumbing and may still occur)."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")  # skip the /api/show max-tokens fallback
    tools = [{"type": "function", "function": {"name": "fn"}}]
    with patch("httpx.Client") as mock_client_cls:
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(ValueError, match="mutually exclusive"):
            client.complete_json("hello", objective="test", tools=tools, schema=_Answer)
    mock_client_cls.assert_not_called()


def test_ollama_schema_forced_starvation_raises_immediately_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema-forced decoding that starves the content channel bails on the FIRST empty
    response — no thinking-downgrade ladder, no second schema-forced attempt."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(200, sse_lines=_REASONING_ONLY_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMSemanticExhaustionError) as exc_info:
            client.complete_json("hello", objective="test", think=True, schema=_Answer)
    assert exc_info.value.schema_forced is True
    assert exc_info.value.attempts_used == 1
    assert exc_info.value.retry_thinking_level is None
    assert waits == []
    assert len(captured) == 1, "schema-forced starvation must not retry the ladder"


def test_ollama_schema_forced_starvation_bypasses_downgrade_ladder_even_with_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schema-forced bail-out takes precedence over LLM_THINKING_DOWNGRADE_RETRY=false too."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_THINKING_DOWNGRADE_RETRY", "false")
    cms = [_stream_cm(200, sse_lines=_REASONING_ONLY_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMSemanticExhaustionError) as exc_info:
            client.complete_json("hello", objective="test", think=True, schema=_Answer)
    assert exc_info.value.schema_forced is True
    assert len(captured) == 1


def test_ollama_schema_ignored_on_continuation_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated response with schema= set continues on plain json_object, not json_schema."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    truncated_sse = [
        'data: {"choices":[{"delta":{"content":"{\\"selected_option_id\\": \\"a\\""},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    continuation_sse = [
        'data: {"choices":[{"delta":{"content":", \\"rationale\\": \\"ok\\"}"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    cms = [
        _stream_cm(200, sse_lines=truncated_sse),
        _stream_cm(200, sse_lines=continuation_sse),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", objective="test", schema=_Answer)
    assert result == {"selected_option_id": "a", "rationale": "ok"}
    assert len(captured) == 2
    assert captured[0]["response_format"]["type"] == "json_schema"
    assert captured[1]["response_format"] == {"type": "json_object"}


def test_normalize_schema_for_wire_accepts_dict_and_pydantic_model() -> None:
    raw = {"type": "object", "properties": {}}
    assert _normalize_schema_for_wire(raw) is raw
    assert _normalize_schema_for_wire(_Answer) == _Answer.model_json_schema()
    with pytest.raises(TypeError):
        _normalize_schema_for_wire(123)
