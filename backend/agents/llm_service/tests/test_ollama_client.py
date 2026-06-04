"""Tests for OllamaLLMClient with mocked httpx."""

import json
from unittest.mock import MagicMock, patch

import pytest

from llm_service.clients.ollama import OllamaLLMClient
from llm_service.interface import LLMPermanentError, LLMRateLimitError


def test_ollama_get_max_context_tokens_known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    client = OllamaLLMClient(
        model="qwen3.5:397b-cloud", base_url="http://localhost:9999", timeout=5
    )
    assert client.get_max_context_tokens() == 262144


def test_ollama_get_max_context_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "50000")
    client = OllamaLLMClient(model="unknown-model", base_url="http://localhost:9999", timeout=5)
    assert client.get_max_context_tokens() == 50000


def _make_show_response(status_code: int, num_ctx: int | None = None) -> MagicMock:
    """Build a mocked /api/show response: 200 with `parameters: "num_ctx N"`, or an error."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"parameters": f"num_ctx {num_ctx}"} if num_ctx is not None else {}
    return resp


def _patch_show(responses: list[MagicMock]) -> tuple[MagicMock, MagicMock]:
    """Return (httpx.Client class mock, shared client instance) whose .post yields `responses` in order."""
    mock_client = MagicMock()
    mock_client.__enter__.return_value.post.side_effect = responses
    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls, mock_client


def test_ollama_known_default_model_skips_api_show(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default deepseek model resolves from the known table and never calls /api/show."""
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    with patch("httpx.Client") as mock_client_cls:
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        assert client.get_max_context_tokens() == 1000000
    mock_client_cls.assert_not_called()


def test_ollama_api_show_transient_failure_does_not_poison(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core regression: a transient /api/show failure must not cache 16384 forever.

    With the fallback TTL at 0s, the next call re-attempts /api/show and, on
    success, resolves the model's real context size instead of staying truncated.
    """
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    monkeypatch.setenv("LLM_NUM_CTX_FALLBACK_TTL_S", "0")
    mock_cls, _ = _patch_show([_make_show_response(500), _make_show_response(200, 262144)])
    with patch("httpx.Client", mock_cls):
        client = OllamaLLMClient(model="unknown-model", base_url="http://localhost:9999", timeout=5)
        assert client.get_max_context_tokens() == 16384  # transient blip → provisional fallback
        assert client.get_max_context_tokens() == 262144  # retried, resolved authoritatively


def test_ollama_api_show_fallback_cached_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Within the TTL, the provisional fallback is reused without re-hammering /api/show."""
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("LLM_NUM_CTX_FALLBACK_TTL_S", raising=False)  # default 300s
    mock_cls, mock_client = _patch_show([_make_show_response(500)])
    with patch("httpx.Client", mock_cls):
        client = OllamaLLMClient(model="unknown-model", base_url="http://localhost:9999", timeout=5)
        assert client.get_max_context_tokens() == 16384
        assert client.get_max_context_tokens() == 16384
    assert mock_client.__enter__.return_value.post.call_count == 1


def test_ollama_api_show_success_is_cached_permanently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolved num_ctx is cached for the process lifetime — no repeat /api/show calls."""
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    mock_cls, mock_client = _patch_show([_make_show_response(200, 262144)])
    with patch("httpx.Client", mock_cls):
        client = OllamaLLMClient(model="unknown-model", base_url="http://localhost:9999", timeout=5)
        assert client.get_max_context_tokens() == 262144
        assert client.get_max_context_tokens() == 262144
    assert mock_client.__enter__.return_value.post.call_count == 1


def _make_streaming_mock(
    status_code: int, sse_lines: list[str] | None = None, body_text: str = ""
) -> tuple:
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


def test_ollama_sse_malformed_chunk_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-JSON SSE chunk is buffered (TCP split recovery) and skipped if still invalid — valid content is preserved."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"{\\"v\\":1}"},"finish_reason":null}]}',
        "data: <not json at all>",
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("test", temperature=0)
        assert result == {"v": 1}


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
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
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


def test_ollama_chat_round_returns_raw_prose_without_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat_round must return plain content as-is and NOT request response_format=json_object.

    Regression: the branding conversation agent used to flow through
    chat_json_round which forced JSON parsing on natural-language replies.
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    prose = (
        "Brandon Kindred — got it. Personal brands live or die on a clear point of view. "
        "What's the work you want to be known for?"
    )
    # Stream the prose one character at a time (after JSON-escaping) to mimic
    # the SSE protocol Ollama uses.
    encoded = json.dumps(prose)  # quoted + escapes wrapped in "..."
    sse_lines = [
        f'data: {{"choices":[{{"delta":{{"content":{encoded}}},"finish_reason":null}}]}}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
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
        result = client.chat(
            [{"role": "user", "content": "tell me about brand strategy"}],
            response_format="text",
            temperature=0.2,
        )
    assert result == prose
    assert captured_payloads, "No payload captured"
    payload = captured_payloads[0]
    # The critical assertion: chat(response_format="text") MUST NOT force JSON output.
    assert "response_format" not in payload
    assert "tools" not in payload


def test_ollama_chat_round_returns_tool_calls_when_tools_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When tools are supplied and the model invokes one, chat_round returns
    a ``__tool_calls__`` dict — same shape as ``chat_json_round`` for tool use."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","type":"function","function":{"name":"do_thing","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "do_thing",
                "description": "Do a thing",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.chat(
            [{"role": "user", "content": "go"}],
            response_format="text",
            tools=tools,
            temperature=0.0,
        )
    assert isinstance(result, dict)
    assert "__tool_calls__" in result
    assert result["__tool_calls__"][0]["function"]["name"] == "do_thing"


def test_ollama_get_max_context_tokens_deepseek_v4_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    """deepseek-v4-pro:cloud has a 1M-token context window; the registry must
    reflect it so context-sizing scales prompts to the real budget instead of
    a quarter of it."""
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    client = OllamaLLMClient(model="deepseek-v4-pro:cloud")
    assert client.get_max_context_tokens() == 1000000


# ---------------------------------------------------------------------------
# Thinking-level resolution on the wire
# ---------------------------------------------------------------------------


def _captured_payload_client(monkeypatch: pytest.MonkeyPatch, model: str) -> tuple:
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    client = OllamaLLMClient(model=model)
    captured: dict = {}

    def fake_post(payload, *args, **kwargs):
        captured.update(payload)
        return '{"ok": true}'

    monkeypatch.setattr(client, "_ollama_post", fake_post)
    return client, captured


def test_complete_json_resolves_default_think_to_max_level(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi")
    assert captured["think"] == "high"


def test_complete_json_explicit_think_false_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", think=False)
    assert captured["think"] is False


def test_complete_resolves_default_think_to_max_level(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete("hi")
    assert captured["think"] == "high"


def test_chat_resolves_default_think_to_max_level(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.chat([{"role": "user", "content": "hi"}])
    assert captured["think"] == "high"


def test_complete_json_boolean_think_for_unregistered_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, captured = _captured_payload_client(monkeypatch, "qwen3.5:cloud")
    client.complete_json("hi")
    assert captured["think"] is True


def test_payload_maps_thinking_level_to_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client posts to the OpenAI-compatible /v1/chat/completions, which
    controls reasoning via reasoning_effort; the native think field is kept
    for proxies that honor it, but levels must also reach reasoning_effort."""
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi")
    assert captured["think"] == "high"
    assert captured["reasoning_effort"] == "high"


def test_payload_omits_reasoning_effort_for_boolean_think(monkeypatch: pytest.MonkeyPatch) -> None:
    """reasoning_effort has no boolean form; unregistered models keep think only."""
    client, captured = _captured_payload_client(monkeypatch, "qwen3.5:cloud")
    client.complete_json("hi")
    assert captured["think"] is True
    assert "reasoning_effort" not in captured


def test_payload_omits_reasoning_effort_when_thinking_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", think=False)
    assert captured["think"] is False
    assert "reasoning_effort" not in captured


def test_chat_and_complete_also_map_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.chat([{"role": "user", "content": "hi"}])
    assert captured["reasoning_effort"] == "high"
    client2, captured2 = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client2.complete("hi")
    assert captured2["reasoning_effort"] == "high"
