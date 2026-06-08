"""Tests for OllamaLLMClient with mocked httpx."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_service.clients.ollama import (
    OllamaLLMClient,
    _parse_retry_after_seconds,
)
from llm_service.interface import LLMPermanentError, LLMRateLimitError, LLMTemporaryError


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
    status_code: int,
    sse_lines: list[str] | None = None,
    body_text: str = "",
    headers: dict | None = None,
) -> tuple:
    """Return (mock_client_cls_instance, mock_stream_response) configured for client.stream() usage."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = body_text
    mock_response.headers = {} if headers is None else headers
    mock_response.read.return_value = None
    if sse_lines is not None:
        mock_response.iter_lines.return_value = iter(sse_lines)

    mock_stream_cm = MagicMock()
    mock_stream_cm.__enter__.return_value = mock_response
    mock_stream_cm.__exit__.return_value = False

    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.return_value = mock_stream_cm
    return mock_client, mock_response


def _stream_cm(
    status_code: int,
    sse_lines: list[str] | None = None,
    body_text: str = "",
    headers: dict | None = None,
    on_exit: "callable | None" = None,
) -> MagicMock:
    """Build a single ``client.stream()`` context manager for one HTTP attempt.

    ``on_exit`` is invoked (if given) when the stream context manager exits, so
    tests can assert ordering between stream teardown and backoff sleeps.
    """
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = body_text
    mock_response.headers = {} if headers is None else headers
    mock_response.read.return_value = None
    if sse_lines is not None:
        mock_response.iter_lines.return_value = iter(sse_lines)

    mock_stream_cm = MagicMock()
    mock_stream_cm.__enter__.return_value = mock_response

    def _exit(*_args: object) -> bool:
        if on_exit is not None:
            on_exit()
        return False

    mock_stream_cm.__exit__.side_effect = _exit
    return mock_stream_cm


def _multi_attempt_client(stream_cms: list[MagicMock]) -> MagicMock:
    """Return a mocked httpx.Client whose successive ``.stream()`` calls yield each cm in order."""
    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.side_effect = list(stream_cms)
    return mock_client


# SSE lines for a successful 200 streaming response returning {"ok": 1}.
_OK_SSE = [
    'data: {"choices":[{"delta":{"content":"{\\"ok\\": 1}"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]",
]


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


# ---------------------------------------------------------------------------
# 429 rate-limit backoff (slow schedule, separate from transient 5xx/network)
# ---------------------------------------------------------------------------


def _patch_no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch the ollama module's time.sleep with a recorder; return the wait log."""
    import llm_service.clients.ollama as ollama_mod

    waits: list[float] = []
    monkeypatch.setattr(ollama_mod.time, "sleep", lambda s: waits.append(s))
    return waits


def test_ollama_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 followed by a 200 retries once on the slow rate-limit schedule (~300s)."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")  # transient budget irrelevant here
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "3600")
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(429, body_text="rate limited"), _stream_cm(200, sse_lines=_OK_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 300.0 <= waits[0] <= 302.0  # first 429 retry must wait at least the 300s floor


def test_ollama_429_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """All 429s: raises LLMRateLimitError after the rate-limit budget, with doubling waits."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "3600")
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(429, body_text="rate limited") for _ in range(3)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.complete_json("hello", temperature=0)
    assert exc_info.value.status_code == 429
    assert len(waits) == 2  # 2 retries before exhausting (3 total attempts)
    assert 300.0 <= waits[0] <= 302.0
    assert 600.0 <= waits[1] <= 602.0


def test_ollama_429_sleep_runs_after_stream_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: the slow 429 sleep must happen AFTER the stream context exits.

    Proves the sleep does not hold the concurrency semaphore / open HTTP stream.
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    events: list[str] = []

    import llm_service.clients.ollama as ollama_mod

    monkeypatch.setattr(ollama_mod.time, "sleep", lambda s: events.append("sleep"))
    cms = [
        _stream_cm(429, body_text="rate limited", on_exit=lambda: events.append("stream_exit")),
        _stream_cm(200, sse_lines=_OK_SSE, on_exit=lambda: events.append("stream_exit")),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert "sleep" in events
    first_sleep = events.index("sleep")
    # The 429 stream must have been torn down before the sleep started.
    assert "stream_exit" in events[:first_sleep]


def test_transient_5xx_schedule_unaffected_by_rate_limit_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 retries on the FAST transient schedule regardless of LLM_RATE_LIMIT_* values."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    monkeypatch.setenv("LLM_BACKOFF_MAX", "120")
    # Aggressive rate-limit settings must NOT leak into the transient path.
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "5")
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(500, body_text="server error"), _stream_cm(200, sse_lines=_OK_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 2.0 <= waits[0] <= 4.0  # fast transient schedule, not 300s


def test_ollama_429_does_not_consume_transient_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interleaved 500 + 429 + 200: each schedule keeps its own independent counter."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    waits = _patch_no_sleep(monkeypatch)
    cms = [
        _stream_cm(500, body_text="server error"),
        _stream_cm(429, body_text="rate limited"),
        _stream_cm(200, sse_lines=_OK_SSE),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    # One fast transient wait (~2s) and one slow rate-limit wait (~300s).
    assert len(waits) == 2
    assert 2.0 <= waits[0] <= 4.0
    assert 300.0 <= waits[1] <= 302.0


def test_ollama_429_honors_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Retry-After larger than the computed backoff extends the wait (capped)."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "3600")
    monkeypatch.setenv("LLM_RATE_LIMIT_HONOR_RETRY_AFTER", "true")
    waits = _patch_no_sleep(monkeypatch)
    cms = [
        _stream_cm(429, body_text="rate limited", headers={"Retry-After": "1200"}),
        _stream_cm(200, sse_lines=_OK_SSE),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert waits[0] >= 1200.0  # Retry-After wins over the 300s computed floor


def test_ollama_429_ignores_retry_after_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With honoring disabled, a Retry-After header is ignored and the schedule is used."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("LLM_RATE_LIMIT_HONOR_RETRY_AFTER", "false")
    waits = _patch_no_sleep(monkeypatch)
    cms = [
        _stream_cm(429, body_text="rate limited", headers={"Retry-After": "1200"}),
        _stream_cm(200, sse_lines=_OK_SSE),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 300.0 <= waits[0] <= 302.0  # header ignored, computed floor used


def test_ollama_5xx_exhaustion_raises_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    """All 500s exhaust the transient budget and raise LLMTemporaryError."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(500, body_text="server error") for _ in range(2)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMTemporaryError) as exc_info:
            client.complete_json("hello", temperature=0)
    assert exc_info.value.status_code == 500
    assert len(waits) == 1  # one retry before exhausting (2 total attempts)


def test_ollama_httpstatuserror_429_uses_rate_limit_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An httpx.HTTPStatusError(429) funnels into the same slow rate-limit schedule."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_MAX", "3600")
    monkeypatch.setenv("LLM_RATE_LIMIT_HONOR_RETRY_AFTER", "true")
    waits = _patch_no_sleep(monkeypatch)
    req = httpx.Request("POST", "http://localhost:9999/v1/chat/completions")
    resp = httpx.Response(429, headers={"Retry-After": "900"}, request=req, text="slow down")
    err = httpx.HTTPStatusError("429", request=req, response=resp)
    mock_client = MagicMock()
    # First .stream() raises HTTPStatusError; second yields a 200 stream.
    mock_client.__enter__.return_value.stream.side_effect = [
        err,
        _stream_cm(200, sse_lines=_OK_SSE),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert waits[0] >= 900.0  # Retry-After from the error response is honored


def test_ollama_httpstatuserror_429_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An httpx.HTTPStatusError(429) raises LLMRateLimitError once the budget is spent."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "0")  # no rate-limit retries
    req = httpx.Request("POST", "http://localhost:9999/v1/chat/completions")
    resp = httpx.Response(429, request=req, text="slow down")
    err = httpx.HTTPStatusError("429", request=req, response=resp)
    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.side_effect = [err]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.complete_json("hello", temperature=0)
    assert exc_info.value.status_code == 429


def test_ollama_empty_content_retries_on_transient_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty 200 body raises LLMTemporaryError and retries on the fast schedule."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    waits = _patch_no_sleep(monkeypatch)
    empty_sse = ['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "data: [DONE]"]
    cms = [_stream_cm(200, sse_lines=empty_sse), _stream_cm(200, sse_lines=_OK_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 2.0 <= waits[0] <= 4.0


def test_ollama_httpstatuserror_5xx_retries_on_transient_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An httpx.HTTPStatusError(500) retries on the fast transient schedule."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    waits = _patch_no_sleep(monkeypatch)
    req = httpx.Request("POST", "http://localhost:9999/v1/chat/completions")
    resp = httpx.Response(500, request=req, text="boom")
    err = httpx.HTTPStatusError("500", request=req, response=resp)
    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.side_effect = [
        err,
        _stream_cm(200, sse_lines=_OK_SSE),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 2.0 <= waits[0] <= 4.0


def test_ollama_connect_error_retries_on_transient_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport ConnectError retries on the fast transient schedule, not the 429 floor."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    waits = _patch_no_sleep(monkeypatch)
    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.side_effect = [
        httpx.ConnectError("boom"),
        _stream_cm(200, sse_lines=_OK_SSE),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 2.0 <= waits[0] <= 4.0  # fast transient schedule, unaffected by rate-limit env


def test_ollama_httpstatuserror_5xx_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An httpx.HTTPStatusError(500) with no transient budget raises LLMTemporaryError."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    req = httpx.Request("POST", "http://localhost:9999/v1/chat/completions")
    resp = httpx.Response(503, request=req, text="unavailable")
    err = httpx.HTTPStatusError("503", request=req, response=resp)
    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.side_effect = [err]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMTemporaryError) as exc_info:
            client.complete_json("hello", temperature=0)
    assert exc_info.value.status_code == 503


def test_ollama_read_timeout_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport ReadTimeout with no transient budget raises LLMTemporaryError."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.side_effect = [httpx.ReadTimeout("slow")]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMTemporaryError):
            client.complete_json("hello", temperature=0)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Retry-After": "120"}, 120.0),
        ({"retry-after": "30"}, 30.0),
        ({}, None),
        ({"Retry-After": "0"}, None),  # non-positive ignored
        ({"Retry-After": "-5"}, None),
        ({"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"}, None),  # HTTP-date form unsupported
        ({"Retry-After": "abc"}, None),
        (None, None),
    ],
)
def test_parse_retry_after_seconds(headers: object, expected: object) -> None:
    assert _parse_retry_after_seconds(headers) == expected


def test_parse_retry_after_seconds_object_without_get() -> None:
    assert _parse_retry_after_seconds(object()) is None


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
    assert captured["think"] == "max"


def test_complete_json_explicit_think_false_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", think=False)
    assert captured["think"] is False


def test_complete_resolves_default_think_to_max_level(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete("hi")
    assert captured["think"] == "max"


def test_chat_resolves_default_think_to_max_level(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.chat([{"role": "user", "content": "hi"}])
    assert captured["think"] == "max"


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
    assert captured["think"] == "max"
    assert captured["reasoning_effort"] == "max"


def test_payload_omits_reasoning_effort_for_boolean_think(monkeypatch: pytest.MonkeyPatch) -> None:
    """reasoning_effort has no boolean form; unregistered models keep think only."""
    client, captured = _captured_payload_client(monkeypatch, "qwen3.5:cloud")
    client.complete_json("hi")
    assert captured["think"] is True
    assert "reasoning_effort" not in captured


def test_chat_and_complete_also_map_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.chat([{"role": "user", "content": "hi"}])
    assert captured["reasoning_effort"] == "max"
    client2, captured2 = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client2.complete("hi")
    assert captured2["reasoning_effort"] == "max"


def test_payload_maps_disabled_thinking_to_reasoning_effort_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama's OpenAI-compatible endpoint controls reasoning via
    reasoning_effort (which supports "none"); think:false alone is ignored
    there, so the kill switch must also be expressed as reasoning_effort."""
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", think=False)
    assert captured["think"] is False
    assert captured["reasoning_effort"] == "none"


def test_global_disable_also_sends_reasoning_effort_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    client.complete_json("hi")
    assert captured["think"] is False
    assert captured["reasoning_effort"] == "none"


def test_tool_call_envelope_carries_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek thinking mode requires the tool-call turn's reasoning_content
    to be passed back on subsequent requests (400 otherwise); the parser must
    surface it on the envelope instead of discarding it."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"step 1; "},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"step 2"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"git_status","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("run status", temperature=0)
    assert result["__tool_calls__"][0]["function"]["name"] == "git_status"
    assert result["__reasoning_content__"] == "step 1; step 2"


def test_tool_call_envelope_omits_empty_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"git_status","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("run status", temperature=0)
    assert "__reasoning_content__" not in result


def test_parser_accumulates_ollama_reasoning_delta_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama's OpenAI-compatible endpoint streams thinking as
    delta.reasoning (openai.go: json:"reasoning,omitempty") — the parser must
    accumulate that dialect too, or the envelope stays empty on the very
    endpoint this client posts to."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    sse_lines = [
        'data: {"choices":[{"delta":{"reasoning":"ollama-style "},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"reasoning":"thinking"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"git_status","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("run status", temperature=0)
    assert result["__reasoning_content__"] == "ollama-style thinking"
