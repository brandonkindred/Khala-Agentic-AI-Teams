"""Tests for OllamaLLMClient with mocked httpx."""

import json
from unittest.mock import MagicMock, patch

import pytest

from llm_service.clients.ollama import OllamaLLMClient
from llm_service.interface import LLMPermanentError, LLMRateLimitError, LLMTemporaryError


def test_ollama_get_max_context_tokens_known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SW_LLM_CONTEXT_SIZE", raising=False)
    client = OllamaLLMClient(
        model="qwen3.5:397b-cloud", base_url="http://localhost:9999", timeout=5
    )
    assert client.get_max_context_tokens() == 262144


def test_ollama_get_max_context_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "50000")
    client = OllamaLLMClient(model="unknown-model", base_url="http://localhost:9999", timeout=5)
    assert client.get_max_context_tokens() == 50000


def _make_streaming_mock(status_code: int, sse_lines: list[str] | None = None, body_text: str = "") -> tuple:
    """Return (mock_client_cls_instance, mock_stream_response) configured for client.stream() usage."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = body_text
    mock_response.read.return_value = None
    if sse_lines is not None:
        mock_response.iter_lines.return_value = iter(sse_lines)

    mock_stream_cm = MagicMock()
    mock_stream_cm.__enter__.return_value = mock_response
    mock_stream_cm.__exit__.return_value = False

    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.return_value = mock_stream_cm
    return mock_client, mock_response


def test_ollama_complete_json_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"{\\"answer\\": 42}"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("What is 6*7?", temperature=0)
    assert result == {"answer": 42}


def test_ollama_streams_and_accumulates_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that content delta chunks are concatenated before JSON parsing."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"{\\"key\\":"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":" \\"value\\"}"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("test", temperature=0)
    assert result == {"key": "value"}


def test_ollama_sse_malformed_chunk_raises_temporary_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-JSON SSE chunk must raise LLMTemporaryError (retriable) instead of being silently dropped."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}',
        "data: <not json at all>",
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMTemporaryError, match="Malformed SSE chunk"):
            client.complete_json("test", temperature=0)


def test_ollama_sse_no_space_after_colon(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE lines with data:{...} (no space after colon) must be parsed correctly."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data:{"choices":[{"delta":{"content":"{\\"v\\":1}"},"finish_reason":null}]}',
        'data:{"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data:[DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("test", temperature=0)
    assert result == {"v": 1}


def test_ollama_complete_json_429_raises_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    mock_client, _ = _make_streaming_mock(429, body_text="Rate limited")
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.complete_json("hello", temperature=0)
        assert exc_info.value.status_code == 429


def test_ollama_complete_json_404_raises_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    mock_client, _ = _make_streaming_mock(404, body_text='{"error":{"message":"model not found"}}')
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMPermanentError) as exc_info:
            client.complete_json("hello", temperature=0)
        assert exc_info.value.status_code == 404


def test_thinking_enabled_for_all_models() -> None:
    """_should_enable_thinking() returns True for any model unless explicitly disabled."""
    client = OllamaLLMClient(model="llama3", base_url="http://localhost:9999", timeout=5)
    assert client._should_enable_thinking() is True

    client_qwen = OllamaLLMClient(model="qwen3.5:397b-cloud", base_url="http://localhost:9999", timeout=5)
    assert client_qwen._should_enable_thinking() is True


def test_thinking_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    client = OllamaLLMClient(model="qwen3.5:397b-cloud", base_url="http://localhost:9999", timeout=5)
    assert client._should_enable_thinking() is False


def test_ollama_tool_call_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming tool_calls deltas are accumulated and returned as __tool_calls__."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":" \\"NYC\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("What's the weather?", tools=tools)
    assert "__tool_calls__" in result
    tc = result["__tool_calls__"][0]
    assert tc["function"]["name"] == "get_weather"
    assert tc["function"]["arguments"] == {"city": "NYC"}


def test_ollama_complete_json_includes_tools_in_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """When tools are passed, payload contains 'tools' and omits 'response_format'."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"fn","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    tools = [{"type": "function", "function": {"name": "fn"}}]
    mock_client, mock_response = _make_streaming_mock(200, sse_lines)
    captured_payloads: list[dict] = []

    original_stream = mock_client.__enter__.return_value.stream

    def capturing_stream(method, url, json=None, headers=None):
        if json is not None:
            captured_payloads.append(json)
        return original_stream(method, url, json=json, headers=headers)

    mock_client.__enter__.return_value.stream = capturing_stream
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        client.complete_json("call fn", tools=tools)
    assert captured_payloads, "No payload captured"
    payload = captured_payloads[0]
    assert "tools" in payload
    assert "response_format" not in payload


def test_extract_json_tolerates_replacement_char_noise() -> None:
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    noisy = '{\n  "approved": false,\n�  "summary": "ok",\n  "feedback_items": []\n}'
    parsed = client._extract_json(noisy)
    assert parsed["approved"] is False
    assert parsed["summary"] == "ok"


def test_extract_json_json_repair_unescaped_quotes_in_strings() -> None:
    """Models often cite JSON/code with unescaped \" inside JSON string values."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    q = chr(34)
    broken_invalid = (
        '{"approved":false,"summary":"Needs fixes","feedback_items":['
        '{"category":"technical",'
        f'"issue":"Displays {q}Resource{q}: {q}*{q} which is wrong",'
        '"suggestion":"Narrow the ARN"}]}'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken_invalid)
    parsed = client._extract_json(broken_invalid)
    assert parsed["approved"] is False
    assert "Resource" in parsed["feedback_items"][0]["issue"]
