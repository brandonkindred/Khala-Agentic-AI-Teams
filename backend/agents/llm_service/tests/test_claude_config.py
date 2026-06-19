"""Tests for Claude-related resolution in llm_service.config."""

from __future__ import annotations

import pytest

from llm_service import config as c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    # No Postgres -> runtime config is empty; env vars are the sole source.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    for var in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_CLAUDE_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_CONTEXT_SIZE",
        "OLLAMA_API_KEY",
        "LLM_OLLAMA_API_KEY",
        "LLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_provider_claude_and_anthropic_alias(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    assert c.resolve_provider() == "claude"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert c.resolve_provider() == "claude"
    monkeypatch.setenv("LLM_PROVIDER", "Claude")  # case-insensitive
    assert c.resolve_provider() == "claude"


def test_provider_defaults_to_ollama():
    assert c.resolve_provider() == "ollama"


def test_resolve_claude_model_precedence(monkeypatch):
    # default
    assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL
    # global
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    assert c.resolve_claude_model(None) == "claude-sonnet-4-6"
    # per-agent override wins
    monkeypatch.setenv("LLM_MODEL_backend", "claude-haiku-4-5")
    assert c.resolve_claude_model("backend") == "claude-haiku-4-5"
    assert c.resolve_claude_model("frontend") == "claude-sonnet-4-6"


def test_resolve_claude_model_ignores_ollama_agent_defaults():
    # 'backend' has an Ollama default in AGENT_DEFAULT_MODELS; Claude must skip it.
    assert c.resolve_claude_model("backend") == c.DEFAULT_CLAUDE_MODEL


def test_looks_like_claude_model():
    for m in (
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-fable-5",
        "claude-mythos-5",
        "anthropic.claude-opus-4-8",
    ):
        assert c._looks_like_claude_model(m) is True
    for m in ("deepseek-v4-pro:cloud", "llama3.1", "qwen3-coder:480b-cloud", "", "gpt-4"):
        assert c._looks_like_claude_model(m) is False


def test_resolve_claude_model_ignores_non_claude_global_env(monkeypatch):
    # The shipped docker default LLM_MODEL=deepseek-v4-pro:cloud must NOT be sent
    # to the Anthropic API — a non-Claude model falls back to the default.
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro:cloud")
    assert c.resolve_claude_model(None) == c.DEFAULT_CLAUDE_MODEL


def test_resolve_claude_model_ignores_non_claude_per_agent_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_backend", "llama3.1")
    assert c.resolve_claude_model("backend") == c.DEFAULT_CLAUDE_MODEL


def test_resolve_claude_model_accepts_claude_global_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-4-5")
    assert c.resolve_claude_model(None) == "claude-haiku-4-5"


def test_resolve_claude_api_key_precedence(monkeypatch):
    assert c.resolve_claude_api_key() == ""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    assert c.resolve_claude_api_key() == "sk-anthropic"
    monkeypatch.setenv("LLM_CLAUDE_API_KEY", "sk-llm")  # Khala-namespaced wins
    assert c.resolve_claude_api_key() == "sk-llm"


def test_resolve_claude_context_size(monkeypatch):
    assert c.resolve_claude_context_size("claude-opus-4-8") == 1_000_000
    assert c.resolve_claude_context_size("claude-haiku-4-5") == 200_000
    assert c.resolve_claude_context_size("unknown-model") == c.DEFAULT_CLAUDE_CONTEXT
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "50000")
    assert c.resolve_claude_context_size("claude-opus-4-8") == 50_000


def test_resolve_ollama_api_key_precedence(monkeypatch):
    assert c.resolve_ollama_api_key() == ""
    monkeypatch.setenv("LLM_OLLAMA_API_KEY", "ll")
    assert c.resolve_ollama_api_key() == "ll"
    monkeypatch.setenv("OLLAMA_API_KEY", "primary")
    assert c.resolve_ollama_api_key() == "primary"


def test_summary_for_claude(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    summary = c.get_llm_config_summary()
    assert "provider=claude" in summary
    assert "model=" in summary
    # never leak keys
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    assert "sk-secret" not in c.get_llm_config_summary()
