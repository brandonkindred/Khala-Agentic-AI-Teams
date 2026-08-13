"""Unit tests for agent_team_studio.agent_provisioning_team.shared.llm_client."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_team_studio.agent_provisioning_team.shared.llm_client import (
    LLMClient,
    LLMRequest,
    sanitize_prompt_var,
)


def test_sanitize_prompt_var_none_returns_empty_string() -> None:
    assert sanitize_prompt_var(None) == ""


def test_sanitize_prompt_var_strips_disallowed_characters() -> None:
    assert sanitize_prompt_var("a<b>c") == "abc"


def test_sanitize_prompt_var_coerces_non_string_input() -> None:
    assert sanitize_prompt_var(42) == "42"


def test_sanitize_prompt_var_within_max_len_is_unchanged() -> None:
    text = "x" * 100
    assert sanitize_prompt_var(text, max_len=200) == text


def test_sanitize_prompt_var_truncates_at_max_len() -> None:
    text = "x" * 300
    out = sanitize_prompt_var(text, max_len=200)
    assert len(out) == 200 + len("…[truncated]")
    assert out.startswith("x" * 200)
    assert out.endswith("…[truncated]")


def test_sanitize_prompt_var_default_max_len_is_100k() -> None:
    text = "x" * 100_001
    out = sanitize_prompt_var(text)
    assert len(out) == 100_000 + len("…[truncated]")


def test_llm_client_is_not_configured_without_model() -> None:
    client = LLMClient(model="")
    assert client.is_configured is False


@pytest.mark.asyncio
async def test_llm_client_complete_falls_back_and_labels_output() -> None:
    client = LLMClient(model="")
    request = LLMRequest(system="s", user=" do the thing ")
    out = await client.complete(request)
    assert out == "[llm-fallback] do the thing"


@pytest.mark.asyncio
async def test_llm_client_complete_logs_fallback_warning_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(LLMClient, "_warned_fallback", False)
    client = LLMClient(model="")
    request = LLMRequest(system="s", user="u")

    with caplog.at_level("WARNING"):
        await client.complete(request)
        await client.complete(request)

    warnings = [r for r in caplog.records if "no LLM model configured" in r.message]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_llm_client_collects_only_native_strands_text_deltas(monkeypatch) -> None:
    captured = {}

    class _NativeModel:
        async def stream(self, messages, *, system_prompt):
            captured["messages"] = messages
            captured["system_prompt"] = system_prompt
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "no"}}}}
            yield {"contentBlockDelta": {"delta": {"text": "hello "}}}
            yield {"contentBlockDelta": {"delta": {"text": "world"}}}
            yield {"metadata": {"usage": {}}}

    client = LLMClient(provider="ollama", model="qwen")
    monkeypatch.setattr(client, "_create_model", lambda request: _NativeModel())

    result = await client.complete(LLMRequest(system="system", user="user"))

    assert result == "hello world"
    assert captured == {
        "messages": [{"role": "user", "content": [{"text": "user"}]}],
        "system_prompt": "system",
    }


def test_llm_client_builds_native_ollama_model() -> None:
    client = LLMClient(
        provider="ollama",
        base_url="https://ollama.example",
        model="qwen",
        api_key="secret",
    )
    request = LLMRequest(system="s", user="u", temperature=0.4, max_tokens=321)

    with patch("strands.models.ollama.OllamaModel") as model_cls:
        assert client._create_model(request) is model_cls.return_value

    model_cls.assert_called_once_with(
        host="https://ollama.example",
        ollama_client_args={"headers": {"Authorization": "Bearer secret"}},
        model_id="qwen",
        temperature=0.4,
        max_tokens=321,
    )


def test_llm_client_builds_native_anthropic_model_for_claude() -> None:
    client = LLMClient(
        provider="claude",
        base_url="https://anthropic.example",
        model="claude-test",
        api_key="secret",
    )
    request = LLMRequest(system="s", user="u", temperature=0.1, max_tokens=456)

    with patch("strands.models.anthropic.AnthropicModel") as model_cls:
        assert client._create_model(request) is model_cls.return_value

    model_cls.assert_called_once_with(
        client_args={"api_key": "secret", "base_url": "https://anthropic.example"},
        model_id="claude-test",
        max_tokens=456,
        params={"temperature": 0.1},
    )


def test_llm_client_rejects_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        LLMClient(provider="unsupported", model="model")
