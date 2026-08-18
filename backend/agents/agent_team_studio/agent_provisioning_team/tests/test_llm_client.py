"""Unit tests for agent_team_studio.agent_provisioning_team.shared.llm_client."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from strands.types.exceptions import ModelThrottledException

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
    monkeypatch.setattr(client, "_create_model", lambda request, config: _NativeModel())

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
        client_args={"api_key": "secret"},
        model_id="claude-test",
        max_tokens=456,
        params={"temperature": 0.1},
    )


def test_llm_client_rejects_empty_claude_key_before_sdk(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-environment-key")
    client = LLMClient(provider="claude", model="claude-test", api_key="")
    request = LLMRequest(system="s", user="u")

    with patch("strands.models.anthropic.AnthropicModel") as model_cls:
        with pytest.raises(ValueError, match="non-empty API key"):
            client._create_model(request)

    model_cls.assert_not_called()


def test_llm_client_resolves_each_call_from_active_provider_entry(monkeypatch) -> None:
    from llm_service import config as llm_config
    from llm_service import provider_store

    entries = iter(
        [
            [
                SimpleNamespace(
                    id=1,
                    provider="ollama",
                    model="qwen",
                    base_url="https://ollama.example",
                    api_key="ollama-key",
                )
            ],
            [
                SimpleNamespace(
                    id=2,
                    provider="claude",
                    model="claude-test",
                    base_url="https://ollama.com",
                    api_key="claude-key",
                )
            ],
        ]
    )
    monkeypatch.setattr(llm_config, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(provider_store, "load_ordered_entries", lambda: next(entries))
    monkeypatch.setattr(provider_store, "select_active_entry", lambda loaded: loaded[0])

    client = LLMClient()
    first = client._resolve_config()
    second = client._resolve_config()

    assert (first.provider, first.model, first.base_url, first.api_key) == (
        "ollama",
        "qwen",
        "https://ollama.example",
        "ollama-key",
    )
    assert (second.provider, second.model, second.base_url, second.api_key) == (
        "claude",
        "claude-test",
        "",
        "claude-key",
    )


def test_llm_client_does_not_use_environment_without_provider_entry(monkeypatch) -> None:
    from llm_service import provider_store

    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("LLM_MODEL", "claude-env")
    monkeypatch.setenv("LLM_CLAUDE_API_KEY", "env-secret")
    monkeypatch.setattr(provider_store, "load_ordered_entries", lambda: [])

    client = LLMClient()

    assert client.is_configured is False
    assert client._resolve_config().api_key == ""


def test_llm_client_uses_shared_defaults_for_blank_ollama_entry(monkeypatch) -> None:
    from llm_service import config as llm_config
    from llm_service import provider_store

    entry = SimpleNamespace(id=1, provider="ollama", model="", base_url="", api_key="")
    monkeypatch.setattr(llm_config, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(
        llm_config,
        "resolve_model_for_provider",
        lambda agent_key, provider: "default-model",
    )
    monkeypatch.setattr(llm_config, "resolve_base_url", lambda: "https://ollama.default")
    monkeypatch.setattr(provider_store, "load_ordered_entries", lambda: [entry])
    monkeypatch.setattr(provider_store, "select_active_entry", lambda loaded: loaded[0])

    config = LLMClient()._resolve_config()

    assert (config.model, config.base_url) == (
        "default-model",
        "https://ollama.default",
    )


def test_llm_client_tolerates_none_fields_on_provider_entry(monkeypatch) -> None:
    from llm_service import config as llm_config
    from llm_service import provider_store

    entry = SimpleNamespace(id=1, provider="ollama", model=None, base_url=None, api_key=None)
    monkeypatch.setattr(llm_config, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(
        llm_config,
        "resolve_model_for_provider",
        lambda agent_key, provider: "default-model",
    )
    monkeypatch.setattr(llm_config, "resolve_base_url", lambda: "https://ollama.default")
    monkeypatch.setattr(provider_store, "load_ordered_entries", lambda: [entry])
    monkeypatch.setattr(provider_store, "select_active_entry", lambda loaded: loaded[0])

    config = LLMClient()._resolve_config()

    assert (config.model, config.base_url, config.api_key) == (
        "default-model",
        "https://ollama.default",
        "",
    )


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "throttling_error",
    [ModelThrottledException("limited"), _StatusError(429)],
    ids=["strands-throttled", "native-status-429"],
)
async def test_llm_client_marks_throttled_entry_and_tries_next_provider(
    monkeypatch, throttling_error
) -> None:
    from llm_service import config as llm_config
    from llm_service import provider_store

    entries = [
        SimpleNamespace(
            id=11,
            provider="ollama",
            model="first-model",
            base_url="https://first.example",
            api_key="first-key",
        ),
        SimpleNamespace(
            id=22,
            provider="claude",
            model="second-model",
            base_url="",
            api_key="second-key",
        ),
    ]
    marks = []
    attempts = []

    class _NativeModel:
        def __init__(self, entry_id):
            self.entry_id = entry_id

        async def stream(self, messages, *, system_prompt):
            if self.entry_id == 11:
                raise throttling_error
            yield {"contentBlockDelta": {"delta": {"text": "from backup"}}}

    monkeypatch.setattr(llm_config, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(llm_config, "failover_rate_window_seconds", lambda: 300)
    monkeypatch.setattr(provider_store, "load_ordered_entries", lambda: entries)
    monkeypatch.setattr(provider_store, "select_active_entry", lambda loaded: loaded[0])
    monkeypatch.setattr(
        provider_store,
        "mark_exhausted",
        lambda entry_id, *, limit_type, reset_at: marks.append(
            (entry_id, limit_type, reset_at)
        ),
    )

    client = LLMClient()

    def create_model(request, config):
        attempts.append(config.entry_id)
        return _NativeModel(config.entry_id)

    monkeypatch.setattr(client, "_create_model", create_model)

    result = await client.complete(LLMRequest(system="system", user="user"))

    assert result == "from backup"
    assert attempts == [11, 22]
    assert [(entry_id, limit_type) for entry_id, limit_type, _ in marks] == [
        (11, "rate")
    ]
    assert marks[0][2].tzinfo is not None


@pytest.mark.asyncio
async def test_llm_client_reraises_last_throttle_when_all_providers_are_limited(
    monkeypatch,
) -> None:
    from llm_service import config as llm_config
    from llm_service import provider_store

    entries = [
        SimpleNamespace(
            id=11,
            provider="ollama",
            model="first-model",
            base_url="https://first.example",
            api_key="",
        ),
        SimpleNamespace(
            id=22,
            provider="claude",
            model="second-model",
            base_url="",
            api_key="second-key",
        ),
    ]
    errors = {
        11: ModelThrottledException("first limited"),
        22: ModelThrottledException("second limited"),
    }
    marked = []

    class _NativeModel:
        def __init__(self, entry_id):
            self.entry_id = entry_id

        async def stream(self, messages, *, system_prompt):
            raise errors[self.entry_id]
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(llm_config, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(llm_config, "failover_rate_window_seconds", lambda: 300)
    monkeypatch.setattr(provider_store, "load_ordered_entries", lambda: entries)
    monkeypatch.setattr(provider_store, "select_active_entry", lambda loaded: loaded[0])
    monkeypatch.setattr(
        provider_store,
        "mark_exhausted",
        lambda entry_id, *, limit_type, reset_at: marked.append(entry_id),
    )

    client = LLMClient()
    monkeypatch.setattr(
        client,
        "_create_model",
        lambda request, config: _NativeModel(config.entry_id),
    )

    with pytest.raises(ModelThrottledException, match="second limited"):
        await client.complete(LLMRequest(system="system", user="user"))

    assert marked == [11, 22]


def test_llm_client_rejects_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        LLMClient(provider="unsupported", model="model")
