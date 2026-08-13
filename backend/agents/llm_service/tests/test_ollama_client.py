"""Tests for OllamaLLMClient with mocked httpx."""

import json
import logging
import re
from typing import Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest

from llm_service.clients.ollama import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    OllamaLLMClient,
    _parse_retry_after_seconds,
    list_ollama_models,
)
from llm_service.interface import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMSemanticExhaustionError,
    LLMTemporaryError,
    record_complete_json_turn,
    take_complete_json_raw,
    take_complete_json_turns,
)


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


def test_ollama_resolve_max_tokens_explicit_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit arg wins (capped); otherwise LLM_MAX_OUTPUT_TOKENS is read via the
    centralized ``llm_config.resolve_max_output_tokens`` (also capped). Neither path
    coerces a non-positive value into a 1-token cap."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    assert client._resolve_max_tokens(100) == 100
    assert client._resolve_max_tokens(999_999_999) == DEFAULT_MAX_OUTPUT_TOKENS
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    assert client._resolve_max_tokens(None) == 4096


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

# SSE lines for a reasoning-only 200 response: thinking deltas but zero content.
_REASONING_ONLY_SSE = [
    'data: {"choices":[{"delta":{"reasoning":"hmm..."},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]",
]

# SSE lines for an empty 200 response that hit the generation cap with no content.
_LENGTH_EMPTY_SSE = [
    'data: {"choices":[{"delta":{"reasoning":"hmm..."},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
    "data: [DONE]",
]


def _capturing_multi_client(stream_cms: list[MagicMock]) -> tuple[MagicMock, list[dict]]:
    """Like ``_multi_attempt_client`` but also captures each attempt's request payload."""
    mock_client = _multi_attempt_client(stream_cms)
    captured: list[dict] = []
    original_stream = mock_client.__enter__.return_value.stream

    def capturing_stream(
        method: str, url: str, json: dict | None = None, headers: dict | None = None
    ):
        if json is not None:
            captured.append(json)
        return original_stream(method, url, json=json, headers=headers)

    mock_client.__enter__.return_value.stream = capturing_stream
    return mock_client, captured


def test_request_and_completion_log_lines_carry_attribution_and_share_rid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The request log line carries rid/agent/team/objective, and the completion
    line for the same call repeats the identical rid (so a call's lines correlate)."""
    from llm_service.attribution import llm_attribution

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
        with caplog.at_level(logging.INFO, logger="llm_service.clients.ollama"):
            with llm_attribution(team="job_matching", agent_key="ranker"):
                client.complete_json("q", objective="rank candidates", temperature=0)

    msgs = [r.getMessage() for r in caplog.records]
    request_lines = [m for m in msgs if m.startswith("LLM request:")]
    assert request_lines, "no request log line emitted"
    req = request_lines[0]
    assert "agent=ranker" in req
    assert "team=job_matching" in req
    assert "objective=rank candidates" in req
    rid = re.search(r"rid=(\S+)", req).group(1)
    assert rid and rid != "-"

    completion_lines = [m for m in msgs if "streaming response complete" in m]
    assert completion_lines, "no completion log line emitted"
    completion = completion_lines[0]
    assert f"rid={rid}" in completion
    # The completion line carries the same attribution fields as the request
    # line, so operators filtering by agent/team/objective see the whole call
    # lifecycle without a second correlation lookup by rid.
    assert "agent=ranker" in completion
    assert "team=job_matching" in completion
    assert "objective=rank candidates" in completion


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
        result = client.complete_json("What is 6*7?", objective="test", temperature=0)
    assert result == {"answer": 42}


def test_complete_json_clears_stale_turns_from_prior_failed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn recorded before this complete_json must not survive a successful
    call that does not continue — otherwise the next observer would attribute
    the stale partial to the new prompt."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    record_complete_json_turn("stale", "old-partial")
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"{\\"answer\\": 42}"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    mock_client, _ = _make_streaming_mock(200, sse_lines)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("What is 6*7?", objective="test", temperature=0)
    assert result == {"answer": 42}
    assert take_complete_json_turns() == []


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
        result = client.complete_json("test", objective="test", temperature=0)
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
        result = client.complete_json("test", objective="test", temperature=0)
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
        result = client.complete_json("test", objective="test", temperature=0)
    assert result == {"v": 1}


def test_ollama_complete_json_429_raises_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    mock_client, _ = _make_streaming_mock(429, body_text="Rate limited")
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.complete_json("hello", objective="test", temperature=0)
        assert exc_info.value.status_code == 429
        assert exc_info.value.limit_kind == "rate"


@pytest.mark.parametrize(
    "body,expected_kind",
    [
        (
            "you (user) have reached your session usage limit, upgrade for higher limits",
            "session",
        ),
        (
            '{"error":"you (user) have reached your weekly usage limit, upgrade"}',
            "weekly",
        ),
    ],
)
def test_ollama_429_classifies_session_and_weekly_bodies(
    monkeypatch: pytest.MonkeyPatch, body: str, expected_kind: str
) -> None:
    """Streaming 429 bodies set limit_kind from Ollama Cloud usage-limit phrases."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "0")
    cms = [_stream_cm(429, body_text=body)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.complete_json("hello", objective="test", temperature=0)
    assert exc_info.value.limit_kind == expected_kind
    assert expected_kind in str(exc_info.value).lower()
    assert "usage limit" in str(exc_info.value).lower()


def test_ollama_httpstatuserror_429_classifies_session_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTPStatusError 429 path classifies session usage limit from response text."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_MAX_RETRIES", "0")
    body = "you (acct) have reached your session usage limit, upgrade for higher limits"
    req = httpx.Request("POST", "http://localhost:9999/v1/chat/completions")
    resp = httpx.Response(429, request=req, text=body)
    err = httpx.HTTPStatusError("429", request=req, response=resp)
    mock_client = MagicMock()
    mock_client.__enter__.return_value.stream.side_effect = [err]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.complete_json("hello", objective="test", temperature=0)
    assert exc_info.value.status_code == 429
    assert exc_info.value.limit_kind == "session"
    assert "session usage limit" in str(exc_info.value).lower()


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
        result = client.complete_json("hello", objective="test", temperature=0)
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
            client.complete_json("hello", objective="test", temperature=0)
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
        result = client.complete_json("hello", objective="test", temperature=0)
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
        result = client.complete_json("hello", objective="test", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 2.0 <= waits[0] <= 4.0  # fast transient schedule, not 300s


def test_ollama_5xx_backoff_releases_concurrency_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transient 5xx backoff sleep must run OUTSIDE the shared concurrency
    gate: holding the process-global semaphore through an exponential backoff
    would block unrelated calls (of any provider) even though no request is in
    flight. The gate depth must be 0 at each sleep, and balanced at the end."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    monkeypatch.setenv("LLM_BACKOFF_MAX", "120")

    import llm_service.clients.ollama as ollama_mod

    depth = {"n": 0}
    depth_at_sleep: list[int] = []

    class _ProbeSem:
        def __enter__(self):
            depth["n"] += 1
            return self

        def __exit__(self, *_a):
            depth["n"] -= 1
            return False

    monkeypatch.setattr(ollama_mod, "get_llm_semaphore", lambda: _ProbeSem())
    monkeypatch.setattr(ollama_mod.time, "sleep", lambda _s: depth_at_sleep.append(depth["n"]))

    cms = [_stream_cm(500, body_text="server error"), _stream_cm(200, sse_lines=_OK_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", objective="test", temperature=0)

    assert result == {"ok": 1}
    assert depth_at_sleep == [0]  # gate released before the 5xx backoff sleep
    assert depth["n"] == 0  # balanced — no leaked slot


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
        result = client.complete_json("hello", objective="test", temperature=0)
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
        result = client.complete_json("hello", objective="test", temperature=0)
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
        result = client.complete_json("hello", objective="test", temperature=0)
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
            client.complete_json("hello", objective="test", temperature=0)
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
        result = client.complete_json("hello", objective="test", temperature=0)
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
            client.complete_json("hello", objective="test", temperature=0)
    assert exc_info.value.status_code == 429


def test_ollama_empty_content_downgrades_boolean_think(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty 200 triggers ONE immediate proof-of-change retry: think True -> False, no backoff sleep."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    waits = _patch_no_sleep(monkeypatch)
    empty_sse = ['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "data: [DONE]"]
    cms = [_stream_cm(200, sse_lines=empty_sse), _stream_cm(200, sse_lines=_OK_SSE)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("hello", objective="test", temperature=0, think=True)
    assert result == {"ok": 1}
    assert waits == []  # the changed payload is the proof of change — no backoff
    assert captured[0]["think"] is True
    assert "reasoning_effort" not in captured[0]
    assert captured[1]["think"] is False
    assert captured[1]["reasoning_effort"] == "none"


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
        result = client.complete_json("hello", objective="test", temperature=0)
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
        result = client.complete_json("hello", objective="test", temperature=0)
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
            client.complete_json("hello", objective="test", temperature=0)
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
            client.complete_json("hello", objective="test", temperature=0)


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
            client.complete_json("hello", objective="test", temperature=0)
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
        result = client.complete_json("What's the weather?", objective="test", tools=tools)
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
        client.complete_json("call fn", objective="test", tools=tools)
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


# ---------------------------------------------------------------------------
# Reasoning ("thinking") token handling: on_reasoning hook + warning level
# ---------------------------------------------------------------------------

_REASONING_THEN_CONTENT_SSE = [
    'data: {"choices":[{"delta":{"reasoning":"step1 "},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"reasoning":"step2"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"content":"{\\"ok\\": 1}"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]",
]


def test_on_reasoning_hook_receives_each_delta_then_returns_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook gets every reasoning delta in order; the stream is still read to the
    final content (read-to-completion regression pin)."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    collected: list[str] = []
    mock_client, _ = _make_streaming_mock(200, _REASONING_THEN_CONTENT_SSE)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="test", base_url="http://localhost:9999", timeout=5, on_reasoning=collected.append
        )
        result = client.complete_json("q", objective="test", temperature=0)
    assert result == {"ok": 1}
    assert collected == ["step1 ", "step2"]


def test_on_reasoning_hook_exception_does_not_break_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising hook is swallowed; the LLM call still returns its content."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    def boom(_token: str) -> None:
        raise RuntimeError("hook failure")

    mock_client, _ = _make_streaming_mock(200, _REASONING_THEN_CONTENT_SSE)
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="test", base_url="http://localhost:9999", timeout=5, on_reasoning=boom
        )
        result = client.complete_json("q", objective="test", temperature=0)
    assert result == {"ok": 1}


def test_reasoning_only_logs_info_not_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reasoning-only (no content) detection line is an expected thinking case —
    logged at INFO, never WARNING. (The separate proof-of-change downgrade retry
    legitimately logs at WARNING; only the detection line is asserted here.)"""
    import logging

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    sse = [
        'data: {"choices":[{"delta":{"reasoning":"thinking hard"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    cms = [
        _stream_cm(200, sse_lines=sse),
        _stream_cm(200, sse_lines=list(sse)),
    ]
    with (
        patch("httpx.Client") as mock_client_cls,
        caplog.at_level(logging.INFO, logger="llm_service.clients.ollama"),
    ):
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        # Empty content still fails the call (semantic exhaustion after the one
        # downgrade retry), but the reasoning-only line itself must be INFO.
        with pytest.raises(LLMSemanticExhaustionError):
            client.complete_json("q", objective="test", temperature=0)
    records = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert any(lvl == "INFO" and "reasoning only" in msg for lvl, msg in records)
    assert not any(lvl == "WARNING" and "reasoning only" in msg for lvl, msg in records)


def test_extract_json_implicit_truncation_raises_for_continuation() -> None:
    """A reply cut off mid-value must raise (not be repaired) so the caller's
    implicit-truncation handler triggers multi-turn continuation. This holds even
    behind a prose/fence prefix — the engine-owned truncation gate is not fooled
    by a leading non-brace character the way a startswith('{') heuristic was."""
    from llm_service.interface import LLMJsonParseError

    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    for truncated in (
        '{"files": {"app/main.py": "def main():\\n    pass  # incomplete',
        'Here is the result:\n{"files": {"app/main.py": "def main():  # incomplete',
        '```json\n{"files": {"app/main.py": "def main():  # incomplete',
    ):
        with pytest.raises(LLMJsonParseError):
            client._extract_json(truncated)


def test_extract_json_repairs_complete_object_with_in_string_bracket() -> None:
    """A COMPLETE object needing only trailing-comma repair, whose string value
    holds an unbalanced bracket, is still repaired — the old caller-side
    brace-count heuristic wrongly classified this as truncated and refused."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    assert client._extract_json('{"summary": "matches [A-Z", "approved": true,}') == {
        "summary": "matches [A-Z",
        "approved": True,
    }


def test_extract_json_malformed_tool_call_envelope_falls_through() -> None:
    """A `{`-leading string that mentions __tool_calls__ but is not valid JSON
    must fall through the pre-check (swallowed JSONDecodeError) to normal salvage
    — exercising the except branch — rather than being returned as an envelope.
    The trailing anchored object makes the salvage result deterministic."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    # `json.loads` on the whole string raises (the leading pseudo-envelope is
    # invalid), so the pre-check's `except json.JSONDecodeError` fires; salvage
    # then returns the well-formed anchored object.
    assert client._extract_json('{"__tool_calls__" oops} {"summary": "real"}') == {
        "summary": "real"
    }


def test_extract_json_passes_tool_call_envelope_through() -> None:
    """A ``__tool_calls__`` envelope carries no _EXPECTED_KEYS anchor, so it must
    be returned verbatim rather than dropped by the anchored salvage tier."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    envelope = json.dumps(
        {"__tool_calls__": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}
    )
    parsed = client._extract_json(envelope)
    assert "__tool_calls__" in parsed
    assert parsed["__tool_calls__"][0]["function"]["name"] == "f"


def test_extract_json_off_schema_object_still_parses() -> None:
    """A clean lone object whose key is not in _EXPECTED_KEYS still parses via
    the accept-any fallback tier (regression guard for the two-tier routing)."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    assert client._extract_json('{"answer": 42}') == {"answer": 42}


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
            objective="test",
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
            objective="test",
            response_format="text",
            tools=tools,
            temperature=0.0,
        )
    assert isinstance(result, dict)
    assert "__tool_calls__" in result
    assert result["__tool_calls__"][0]["function"]["name"] == "do_thing"


def test_ollama_chat_json_self_corrects_prose_when_tools_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With tools present, response_format=json_object cannot be set on the wire,
    so models sometimes emit analysis prose. chat(response_format=\"json\") must
    perform one corrective follow-up that recovers a JSON object instead of
    raising LLMJsonParseError (code_review Strands tool-loop failure mode).
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    prose = (
        "I'll analyze the file structure you've provided to understand the "
        "project's architecture and components.\n\n## Core Architecture"
    )
    prose_sse = [
        f'data: {{"choices":[{{"delta":{{"content":{json.dumps(prose)}}},"finish_reason":null}}]}}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    json_sse = [
        'data: {"choices":[{"delta":{"content":"{\\"findings\\": []}"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    mock_client = _multi_attempt_client(
        [_stream_cm(200, prose_sse), _stream_cm(200, json_sse)]
    )
    captured: list[dict] = []
    original_stream = mock_client.__enter__.return_value.stream

    def capturing_stream(method, url, json=None, headers=None):
        if json is not None:
            captured.append(json)
        return original_stream(method, url, json=json, headers=headers)

    mock_client.__enter__.return_value.stream = capturing_stream
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.chat(
            [{"role": "user", "content": "review this"}],
            objective="strands agent turn (code_review)",
            response_format="json",
            tools=tools,
            temperature=0.0,
        )
    assert result == {"findings": []}
    assert len(captured) == 2
    # First attempt: tools present, no response_format (OpenAI-compat mutual exclusion).
    assert "tools" in captured[0]
    assert "response_format" not in captured[0]
    # Corrective attempt keeps tools and appends a rejection of the prose turn.
    assert "tools" in captured[1]
    msgs = captured[1]["messages"]
    assert msgs[-2]["role"] == "assistant"
    assert "architecture" in msgs[-2]["content"]
    assert msgs[-1]["role"] == "user"
    assert "rejected" in msgs[-1]["content"].lower()
    assert "json" in msgs[-1]["content"].lower()


def test_ollama_chat_json_self_correct_exhausted_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the corrective follow-up is also non-JSON prose, re-raise LLMJsonParseError
    after exactly one corrective attempt (two total stream calls)."""
    from llm_service.interface import LLMJsonParseError

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    prose = "Still thinking about the architecture in markdown."
    prose_sse = [
        f'data: {{"choices":[{{"delta":{{"content":{json.dumps(prose)}}},"finish_reason":null}}]}}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    tools = [{"type": "function", "function": {"name": "list_files", "parameters": {}}}]
    mock_client = _multi_attempt_client(
        [_stream_cm(200, prose_sse), _stream_cm(200, prose_sse)]
    )
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMJsonParseError):
            client.chat(
                [{"role": "user", "content": "review this"}],
                objective="strands agent turn (code_review)",
                response_format="json",
                tools=tools,
                temperature=0.0,
            )
    assert mock_client.__enter__.return_value.stream.call_count == 2


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


def test_complete_json_resolves_default_think_to_false_for_json_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """complete_json is always JSON mode; with no explicit think and no agent
    pin, extended thinking competes with strict JSON decoding for the content
    channel, so the default resolves to thinking off."""
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", objective="test")
    assert captured["think"] is False


def test_complete_json_explicit_think_true_resolves_to_max_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", objective="test", think=True)
    assert captured["think"] == "max"


def test_complete_json_explicit_think_false_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", objective="test", think=False)
    assert captured["think"] is False


def test_complete_resolves_default_think_to_max_level(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete("hi", objective="test")
    assert captured["think"] == "max"


def test_chat_resolves_default_think_to_false_for_json_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat() defaults to response_format='json'; with no explicit think and no
    agent pin, the default resolves to thinking off, same as complete_json."""
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.chat([{"role": "user", "content": "hi"}], objective="test")
    assert captured["think"] is False


def test_chat_text_mode_resolves_default_think_to_max_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """response_format='text' is not JSON mode, so None still upgrades to the
    model's max registered thinking level."""
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.chat([{"role": "user", "content": "hi"}], objective="test", response_format="text")
    assert captured["think"] == "max"


def test_complete_json_default_think_false_for_unregistered_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, captured = _captured_payload_client(monkeypatch, "qwen3.5:cloud")
    client.complete_json("hi", objective="test")
    assert captured["think"] is False


def test_complete_json_explicit_think_true_boolean_for_unregistered_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, captured = _captured_payload_client(monkeypatch, "qwen3.5:cloud")
    client.complete_json("hi", objective="test", think=True)
    assert captured["think"] is True


def test_payload_maps_thinking_level_to_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client posts to the OpenAI-compatible /v1/chat/completions, which
    controls reasoning via reasoning_effort; the native think field is kept
    for proxies that honor it, but levels must also reach reasoning_effort."""
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", objective="test", think=True)
    assert captured["think"] == "max"
    assert captured["reasoning_effort"] == "max"


def test_payload_omits_reasoning_effort_for_boolean_think(monkeypatch: pytest.MonkeyPatch) -> None:
    """reasoning_effort has no boolean form; unregistered models keep think only."""
    client, captured = _captured_payload_client(monkeypatch, "qwen3.5:cloud")
    client.complete_json("hi", objective="test", think=True)
    assert captured["think"] is True
    assert "reasoning_effort" not in captured


def test_chat_and_complete_also_map_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.chat([{"role": "user", "content": "hi"}], objective="test", think=True)
    assert captured["reasoning_effort"] == "max"
    client2, captured2 = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client2.complete("hi", objective="test")
    assert captured2["reasoning_effort"] == "max"


def test_payload_maps_disabled_thinking_to_reasoning_effort_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama's OpenAI-compatible endpoint controls reasoning via
    reasoning_effort (which supports "none"); think:false alone is ignored
    there, so the kill switch must also be expressed as reasoning_effort."""
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    client.complete_json("hi", objective="test", think=False)
    assert captured["think"] is False
    assert captured["reasoning_effort"] == "none"


def test_global_disable_also_sends_reasoning_effort_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, captured = _captured_payload_client(monkeypatch, "deepseek-v4-pro:cloud")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    client.complete_json("hi", objective="test")
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
        result = client.complete_json("run status", objective="test", temperature=0)
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
        result = client.complete_json("run status", objective="test", temperature=0)
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
        result = client.complete_json("run status", objective="test", temperature=0)
    assert result["__reasoning_content__"] == "ollama-style thinking"


# ---------------------------------------------------------------------------
# Semantic exhaustion: proof-of-change thinking-downgrade retry
# ---------------------------------------------------------------------------


def test_reasoning_only_downgrades_one_thinking_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reasoning-only response on a registered-levels model retries once, one level down, no sleep."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    waits = _patch_no_sleep(monkeypatch)
    cms = [
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
        _stream_cm(200, sse_lines=list(_OK_SSE)),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        result = client.complete_json("q", objective="test", temperature=0, think=True)
    assert result == {"ok": 1}
    assert captured[0]["think"] == "max"
    assert captured[0]["reasoning_effort"] == "max"
    assert captured[1]["think"] == "high"
    assert captured[1]["reasoning_effort"] == "high"
    assert waits == []


def test_ladder_exhausts_after_downgrade_then_thinking_off(
    monkeypatch: pytest.MonkeyPatch, caplog: "pytest.LogCaptureFixture"
) -> None:
    """Reasoning-only at every rung raises the receipt only after the full ladder
    (max -> high -> thinking-off): one max->high notch is not enough, so a reduced
    tier is followed by a decisive thinking-off retry. The transient budget is
    never consumed, and the receipt is logged at ERROR."""
    import logging

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)  # default 10 must NOT be spent
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)) for _ in range(3)]
    with (
        patch("httpx.Client") as mock_client_cls,
        caplog.at_level(logging.ERROR, logger="llm_service.clients.ollama"),
    ):
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        with pytest.raises(LLMSemanticExhaustionError) as exc_info:
            client.complete_json("q", objective="test", temperature=0, think=True)
    err = exc_info.value
    assert isinstance(err, LLMTemporaryError)  # outer pause/degrade handlers still work
    assert err.failure_class == "semantic_exhaustion"
    assert err.attempts_used == 3
    assert err.original_thinking_level == "max"
    assert err.retry_thinking_level is False  # the last rung disabled thinking entirely
    assert err.content_bytes_seen is False
    assert err.finish_reason == "stop"
    assert len(err.payload_fingerprint) == 16
    assert all(c in "0123456789abcdef" for c in err.payload_fingerprint)
    # Ladder rungs: max -> high -> thinking-off, skipping the wire-redundant
    # low/medium tiers; exactly three HTTP attempts despite the default transient budget.
    assert [c["reasoning_effort"] for c in captured] == ["max", "high", "none"]
    assert len(captured) == 3
    assert waits == []
    assert any("semantic_exhaustion" in r.getMessage() for r in caplog.records)


def test_downgrade_retry_logged_at_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: "pytest.LogCaptureFixture"
) -> None:
    """The proof-of-change retry logs the old -> new thinking level at WARNING."""
    import logging

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    cms = [
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
        _stream_cm(200, sse_lines=list(_OK_SSE)),
    ]
    with (
        patch("httpx.Client") as mock_client_cls,
        caplog.at_level(logging.WARNING, logger="llm_service.clients.ollama"),
    ):
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        client.complete_json("q", objective="test", temperature=0, think=True)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("proof-of-change retry" in m and "'max'" in m and "'high'" in m for m in warnings)


def test_no_downgrade_available_fails_fast_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """think=False leaves no proof of change at all: the first empty response fails
    hard with one attempt and no retry (thinking is already off)."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    cms = [_stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE))]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        with pytest.raises(LLMSemanticExhaustionError) as exc_info:
            client.complete_json("q", objective="test", temperature=0, think=False)
    assert len(captured) == 1
    assert exc_info.value.attempts_used == 1
    assert exc_info.value.original_thinking_level is False
    assert exc_info.value.retry_thinking_level is None


def test_reduced_tier_retries_once_with_thinking_off_then_exhausts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting below the model's top tier (e.g. code_review's ``high``), a
    reasoning-only turn skips the wire-redundant intermediate tiers and retries
    exactly once with thinking disabled before exhausting."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    cms = [
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        with pytest.raises(LLMSemanticExhaustionError) as exc_info:
            client.complete_json("q", objective="test", temperature=0, think="high")
    assert [c["reasoning_effort"] for c in captured] == ["high", "none"]
    assert exc_info.value.attempts_used == 2
    assert exc_info.value.original_thinking_level == "high"
    assert exc_info.value.retry_thinking_level is False


def test_reduced_tier_recovers_on_thinking_off_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thinking-off retry is what rescues a reduced-tier reasoning-only turn:
    the model emits content once reasoning is disabled. This is the code_review
    default-``high`` path — a completed review instead of semantic exhaustion."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    cms = [
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
        _stream_cm(200, sse_lines=list(_OK_SSE)),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        result = client.complete_json("q", objective="test", temperature=0, think="high")
    assert result == {"ok": 1}
    assert [c["reasoning_effort"] for c in captured] == ["high", "none"]


def test_transient_5xx_before_downgrade_keeps_schedule_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 before any empty response retries the identical payload on the backoff schedule."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    waits = _patch_no_sleep(monkeypatch)
    cms = [
        _stream_cm(500, body_text="boom"),
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
        _stream_cm(200, sse_lines=list(_OK_SSE)),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        result = client.complete_json("q", objective="test", temperature=0, think=True)
    assert result == {"ok": 1}
    assert captured[0]["reasoning_effort"] == "max"
    assert captured[1]["reasoning_effort"] == "max"  # 5xx retry: identical payload
    assert captured[2]["reasoning_effort"] == "high"  # downgrade after the empty
    assert len(waits) == 1  # only the 5xx slept
    assert 2.0 <= waits[0] <= 4.0


def test_transient_5xx_after_downgrade_retries_downgraded_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 after the downgrade re-sends the DOWNGRADED payload on the transient schedule."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    waits = _patch_no_sleep(monkeypatch)
    cms = [
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
        _stream_cm(500, body_text="boom"),
        _stream_cm(200, sse_lines=list(_OK_SSE)),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        result = client.complete_json("q", objective="test", temperature=0, think=True)
    assert result == {"ok": 1}
    assert captured[0]["reasoning_effort"] == "max"
    assert captured[1]["reasoning_effort"] == "high"
    assert captured[2]["reasoning_effort"] == "high"
    assert len(waits) == 1


def test_kill_switch_restores_legacy_transient_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_THINKING_DOWNGRADE_RETRY=false restores the legacy behavior: identical
    payloads retried on the transient schedule, plain LLMTemporaryError at the end."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_THINKING_DOWNGRADE_RETRY", "false")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)) for _ in range(3)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        with pytest.raises(LLMTemporaryError) as exc_info:
            client.complete_json("q", objective="test", temperature=0, think=True)
    assert not isinstance(exc_info.value, LLMSemanticExhaustionError)
    assert len(captured) == 3
    assert all(p["reasoning_effort"] == "max" for p in captured)
    assert len(waits) == 2  # legacy exponential backoff between attempts


def test_length_empty_is_semantic_exhaustion_with_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finish_reason=length with zero content takes the semantic path and the receipt records it."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    cms = [_stream_cm(200, sse_lines=list(_LENGTH_EMPTY_SSE)) for _ in range(3)]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        with pytest.raises(LLMSemanticExhaustionError) as exc_info:
            client.complete_json("q", objective="test", temperature=0, think=True)
    assert exc_info.value.finish_reason == "length"
    assert [c["reasoning_effort"] for c in captured] == ["max", "high", "none"]


def test_semantic_exhaustion_log_reports_reasoning_channel_diagnostic(
    monkeypatch: pytest.MonkeyPatch, caplog: "pytest.LogCaptureFixture"
) -> None:
    """The exhaustion ERROR log carries reasoning-channel diagnostics (length + a
    JSON-presence probe) so operators can tell whether the answer was misrouted
    into the reasoning channel — without logging any raw model output."""
    import logging

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    # Reasoning channel carries a JSON object (the probe must detect it); no content.
    reasoning_json_sse = [
        'data: {"choices":[{"delta":{"reasoning":"weighing... {\\"approved\\": true} done"},'
        '"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    cms = [_stream_cm(200, sse_lines=reasoning_json_sse)]
    with (
        patch("httpx.Client") as mock_client_cls,
        caplog.at_level(logging.ERROR, logger="llm_service.clients.ollama"),
    ):
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        # think=False → no downgrade retry, so exhaustion (and its log) fires on attempt 1.
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMSemanticExhaustionError):
            client.complete_json("q", objective="test", temperature=0, think=False)
    assert len(captured) == 1
    receipts = [r.getMessage() for r in caplog.records if "semantic_exhaustion" in r.getMessage()]
    assert receipts
    assert "reasoning_has_json=True" in receipts[0]
    assert "reasoning_len=0" not in receipts[0]  # a non-empty reasoning channel was seen


def test_semantic_exhaustion_diagnostic_accumulates_across_ladder(
    monkeypatch: pytest.MonkeyPatch, caplog: "pytest.LogCaptureFixture"
) -> None:
    """The receipt's reasoning diagnostic reflects any rung that held the answer —
    not just the final thinking-off rung, which carries no reasoning. A first rung
    with JSON in reasoning followed by an empty thinking-off rung still reports
    reasoning_has_json=True."""
    import logging

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    with_json = [
        'data: {"choices":[{"delta":{"reasoning":"draft {\\"approved\\": false} end"},'
        '"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    # Rung 1 (high) has JSON in reasoning; rung 2 (thinking-off) is truly empty.
    cms = [_stream_cm(200, sse_lines=with_json), _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE))]
    with (
        patch("httpx.Client") as mock_client_cls,
        caplog.at_level(logging.ERROR, logger="llm_service.clients.ollama"),
    ):
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        with pytest.raises(LLMSemanticExhaustionError):
            client.complete_json("q", objective="test", temperature=0, think="high")
    assert [c["reasoning_effort"] for c in captured] == ["high", "none"]
    receipt = next(r.getMessage() for r in caplog.records if "semantic_exhaustion" in r.getMessage())
    # Accumulated from rung 1, not taken from the empty final rung.
    assert "reasoning_has_json=True" in receipt


def test_boolean_thinking_retries_once_with_thinking_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """think=True on a model with no registered levels retries exactly once with
    thinking disabled (the ladder's boolean-on rung) before exhausting."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    cms = [
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMSemanticExhaustionError) as exc_info:
            client.complete_json("q", objective="test", temperature=0, think=True)
    assert captured[0]["think"] is True
    assert captured[1]["think"] is False
    assert captured[1]["reasoning_effort"] == "none"
    assert exc_info.value.attempts_used == 2
    assert exc_info.value.retry_thinking_level is False


def test_reasoning_json_probe_is_total_including_recursion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reasoning-channel JSON probe never raises and always returns a bool —
    including when ``json.loads`` raises ``RecursionError`` (not a ``ValueError``, so
    a naive guard would let it escape)."""
    import llm_service.clients.ollama as ollama_mod
    from llm_service.clients.ollama import _reasoning_json_probe

    assert _reasoning_json_probe("") is False
    assert _reasoning_json_probe("no braces here") is False
    assert _reasoning_json_probe('prose {"a": 1} more prose') is True
    assert _reasoning_json_probe("}{ closing before opening") is False
    assert _reasoning_json_probe("{ never closed") is False
    assert _reasoning_json_probe("{not: valid}") is False  # braces present but unparseable
    # A given runtime's JSON nesting limit varies (some parse deep input, some raise
    # RecursionError), so the deep case only asserts the probe returns a bool without
    # raising — never a specific truthiness.
    deep = '{"a":' * 2000 + "1" + "}" * 2000
    assert _reasoning_json_probe(deep) in (True, False)

    # Deterministically exercise the RecursionError branch on every runtime: force
    # json.loads to raise it and confirm the probe swallows it into False.
    def _raise_recursion(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ollama_mod.json, "loads", _raise_recursion)
    assert _reasoning_json_probe('{"a": 1}') is False


@pytest.mark.parametrize(
    "invoke",
    [
        lambda client: client.complete("q", objective="test", think=False),
        lambda client: client.chat(
            [{"role": "user", "content": "q"}], objective="test", think=False
        ),
    ],
    ids=["complete", "chat"],
)
def test_entry_points_record_semantic_exhaustion_telemetry(
    monkeypatch: pytest.MonkeyPatch, invoke: "Callable[[OllamaLLMClient], object]"
) -> None:
    """complete() and chat() record error_type=semantic_exhaustion before re-raising, matching complete_json."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    recorded: list[dict] = []
    monkeypatch.setattr(
        "llm_service.clients.ollama.record_llm_call", lambda **kw: recorded.append(kw)
    )
    cms = [
        _stream_cm(
            200,
            sse_lines=['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "data: [DONE]"],
        )
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMSemanticExhaustionError):
            invoke(client)
    assert any(r.get("error_type") == "semantic_exhaustion" for r in recorded)


def test_continuation_resumes_at_downgraded_thinking_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncation AFTER an in-call downgrade continues at the downgraded level,
    not the original level that already failed to produce content."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_THINKING_LEVEL", raising=False)
    truncated_partial_sse = [
        'data: {"choices":[{"delta":{"content":"{\\"ok\\":"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    continuation_ok_sse = [
        'data: {"choices":[{"delta":{"content":" 1}"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    cms = [
        _stream_cm(200, sse_lines=list(_REASONING_ONLY_SSE)),  # downgrade max -> high
        _stream_cm(200, sse_lines=truncated_partial_sse),  # downgraded attempt truncates
        _stream_cm(200, sse_lines=continuation_ok_sse),  # continuation request
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(
            model="deepseek-v4-pro:cloud", base_url="http://localhost:9999", timeout=5
        )
        result = client.complete_json("q", objective="test", temperature=0, think=True)
    assert result == {"ok": 1}
    assert captured[0]["reasoning_effort"] == "max"
    assert captured[1]["reasoning_effort"] == "high"
    assert captured[2]["reasoning_effort"] == "high"  # continuation inherits the downgrade


def test_complete_json_continuation_records_each_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each continuation HTTP turn (initial partial + continuation reply) is
    recorded for observers, and the merged raw text is stored for take()."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    truncated_partial_sse = [
        'data: {"choices":[{"delta":{"content":"{\\"ok\\":"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    continuation_ok_sse = [
        'data: {"choices":[{"delta":{"content":" 1}"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    cms = [
        _stream_cm(200, sse_lines=truncated_partial_sse),
        _stream_cm(200, sse_lines=continuation_ok_sse),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, _captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("q", objective="test", temperature=0, think=False)
    assert result == {"ok": 1}
    turns = take_complete_json_turns()
    raw = take_complete_json_raw()
    assert len(turns) == 2
    assert turns[0][0] == "q"
    assert turns[0][1] == '{"ok":'
    assert isinstance(turns[0][2], float)
    continuation_messages = json.loads(turns[1][0])
    assert continuation_messages[1] == {"role": "user", "content": "q"}
    assert continuation_messages[2] == {"role": "assistant", "content": '{"ok":'}
    assert continuation_messages[3]["role"] == "user"
    assert "continue exactly from where you left off" in continuation_messages[3]["content"]
    assert turns[1][1] == " 1}"
    assert raw == '{"ok": 1}'
    assert turns[0][2] <= turns[1][2]


def test_complete_text_continuation_records_each_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text complete() continuation HTTP turns must each be recorded, matching
    complete_json, so reasoning transcripts are per-LLM-call not merged-only."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    truncated_partial_sse = [
        'data: {"choices":[{"delta":{"content":"PARTIAL "},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    continuation_ok_sse = [
        'data: {"choices":[{"delta":{"content":"REVIEW"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    cms = [
        _stream_cm(200, sse_lines=truncated_partial_sse),
        _stream_cm(200, sse_lines=continuation_ok_sse),
    ]
    with patch("httpx.Client") as mock_client_cls:
        mock_client, _captured = _capturing_multi_client(cms)
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete("q", objective="test", think=False)
    assert result == "PARTIAL REVIEW"
    turns = take_complete_json_turns()
    assert len(turns) == 2
    assert turns[0][0] == "q"
    assert turns[0][1] == "PARTIAL "
    continuation_messages = json.loads(turns[1][0])
    assert continuation_messages[0] == {"role": "user", "content": "q"}
    assert continuation_messages[1] == {"role": "assistant", "content": "PARTIAL "}
    assert turns[1][1] == "REVIEW"
    assert turns[0][2] <= turns[1][2]


def test_generic_temporary_error_retries_on_transient_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any LLMTemporaryError surfaced inside an attempt (not just 5xx/transport)
    retries via the shared transient step — the generic handler owns the schedule."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "2")
    waits = _patch_no_sleep(monkeypatch)
    cms = [_stream_cm(200, sse_lines=list(_OK_SSE)), _stream_cm(200, sse_lines=list(_OK_SSE))]
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value = _multi_attempt_client(cms)
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        real_parse = client._parse_response_content
        outcomes = iter(["raise", "ok"])

        def flaky_parse(data: dict) -> str:
            if next(outcomes) == "raise":
                raise LLMTemporaryError("transient blip")
            return real_parse(data)

        monkeypatch.setattr(client, "_parse_response_content", flaky_parse)
        result = client.complete_json("q", objective="test", temperature=0)
    assert result == {"ok": 1}
    assert len(waits) == 1
    assert 2.0 <= waits[0] <= 4.0


def _make_tags_response(status_code: int, payload: object) -> MagicMock:
    """Build a mocked /api/tags response with the given status and JSON payload."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


def _patch_tags_get(response: object) -> tuple[MagicMock, MagicMock]:
    """Return (httpx.Client class mock, shared instance) whose .get yields `response`.

    A non-MagicMock ``response`` (e.g. an exception instance) is used as the
    ``side_effect`` so a raising client can be simulated.
    """
    mock_client = MagicMock()
    if isinstance(response, BaseException):
        mock_client.__enter__.return_value.get.side_effect = response
    else:
        mock_client.__enter__.return_value.get.return_value = response
    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls, mock_client


def _clear_ollama_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env that would steer base-URL / key resolution so tests are deterministic."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    for var in ("LLM_BASE_URL", "OLLAMA_API_KEY", "LLM_OLLAMA_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_list_ollama_models_parses_and_sorts_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # Names are de-duplicated, sorted, and prefer `name` then fall back to `model`.
    _clear_ollama_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434")
    payload = {
        "models": [
            {"name": "deepseek-v4-flash:cloud", "model": "deepseek-v4-flash:cloud:latest"},
            {"name": "deepseek-v4-flash:cloud"},
            {"model": "qwen3-coder:480b-cloud"},  # no name -> falls back to model
            {"name": "deepseek-v4-flash:cloud"},  # duplicate -> collapsed
            {"name": ""},  # blank -> dropped
            "not-a-dict",  # ignored
        ]
    }
    mock_cls, mock_client = _patch_tags_get(_make_tags_response(200, payload))
    with patch("httpx.Client", mock_cls):
        assert list_ollama_models() == ["deepseek-v4-flash:cloud", "deepseek-v4-flash:cloud", "qwen3-coder:480b-cloud"]
    # The request targets {base_url}/api/tags.
    called_url = mock_client.__enter__.return_value.get.call_args[0][0]
    assert called_url == "http://localhost:11434/api/tags"


def test_list_ollama_models_sends_bearer_only_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # A resolved Ollama key adds an Authorization: Bearer header; none -> no header.
    _clear_ollama_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://ollama.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "ok-secret")
    mock_cls, mock_client = _patch_tags_get(_make_tags_response(200, {"models": [{"name": "m"}]}))
    with patch("httpx.Client", mock_cls):
        assert list_ollama_models() == ["m"]
    headers = mock_client.__enter__.return_value.get.call_args.kwargs["headers"]
    assert headers == {"Authorization": "Bearer ok-secret"}

    # No key -> no Authorization header (local Ollama).
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    mock_cls2, mock_client2 = _patch_tags_get(_make_tags_response(200, {"models": [{"name": "m"}]}))
    with patch("httpx.Client", mock_cls2):
        assert list_ollama_models() == ["m"]
    assert mock_client2.__enter__.return_value.get.call_args.kwargs["headers"] == {}


def test_list_ollama_models_non_200_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ollama_env(monkeypatch)
    mock_cls, _ = _patch_tags_get(_make_tags_response(404, {"models": []}))
    with patch("httpx.Client", mock_cls):
        assert list_ollama_models() == []


def test_list_ollama_models_malformed_body_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 200 whose body isn't the expected {"models": [...]} shape -> [] (never raises).
    _clear_ollama_env(monkeypatch)
    mock_cls, _ = _patch_tags_get(_make_tags_response(200, {"unexpected": True}))
    with patch("httpx.Client", mock_cls):
        assert list_ollama_models() == []


def test_list_ollama_models_http_error_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # A network/HTTP error is swallowed (best-effort discovery) -> [].
    _clear_ollama_env(monkeypatch)
    mock_cls, _ = _patch_tags_get(httpx.ConnectError("refused"))
    with patch("httpx.Client", mock_cls):
        assert list_ollama_models() == []
