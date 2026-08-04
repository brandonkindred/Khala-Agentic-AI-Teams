"""Tests for the failover config resolvers and the per-client 429-retry override."""

from __future__ import annotations

import pytest

from llm_service import config as llm_config
from llm_service.clients import ClaudeLLMClient, OllamaLLMClient


def test_fast_429_default_on(monkeypatch):
    """With the env var unset, fast-fail-on-429 defaults to enabled."""
    monkeypatch.delenv("LLM_FAILOVER_FAST_429", raising=False)
    assert llm_config.failover_fast_429_enabled() is True


@pytest.mark.parametrize(
    "val,expected", [("false", False), ("0", False), ("true", True), ("1", True)]
)
def test_fast_429_env_override(monkeypatch, val, expected):
    """LLM_FAILOVER_FAST_429 explicitly toggles the fast-fail default off/on."""
    monkeypatch.setenv("LLM_FAILOVER_FAST_429", val)
    assert llm_config.failover_fast_429_enabled() is expected


def test_window_defaults_and_overrides(monkeypatch):
    """Rate/session/weekly fallback windows default sensibly and honor a positive env
    override; non-positive or unparseable overrides fall back to the default."""
    monkeypatch.delenv("LLM_FAILOVER_RATE_WINDOW_S", raising=False)
    monkeypatch.delenv("LLM_FAILOVER_SESSION_WINDOW_S", raising=False)
    monkeypatch.delenv("LLM_FAILOVER_WEEKLY_WINDOW_S", raising=False)
    assert llm_config.failover_rate_window_seconds() == 300.0
    assert llm_config.failover_session_window_seconds() == 65 * 60.0
    assert llm_config.failover_weekly_window_seconds() == 24 * 3600.0
    monkeypatch.setenv("LLM_FAILOVER_RATE_WINDOW_S", "900")
    assert llm_config.failover_rate_window_seconds() == 900.0
    monkeypatch.setenv("LLM_FAILOVER_SESSION_WINDOW_S", "7200")
    assert llm_config.failover_session_window_seconds() == 7200.0
    # Non-positive / garbage falls back to the default.
    monkeypatch.setenv("LLM_FAILOVER_RATE_WINDOW_S", "-5")
    assert llm_config.failover_rate_window_seconds() == 300.0
    monkeypatch.setenv("LLM_FAILOVER_SESSION_WINDOW_S", "garbage")
    assert llm_config.failover_session_window_seconds() == 65 * 60.0
    monkeypatch.setenv("LLM_FAILOVER_WEEKLY_WINDOW_S", "garbage")
    assert llm_config.failover_weekly_window_seconds() == 24 * 3600.0
    monkeypatch.setenv("LLM_FAILOVER_WEEKLY_WINDOW_S", "172800")
    assert llm_config.failover_weekly_window_seconds() == 172800.0


def test_ollama_rate_limit_override_applies(monkeypatch):
    """An explicit rate_limit_max_retries override wins; omitting it uses the env schedule."""
    monkeypatch.delenv("LLM_RATE_LIMIT_MAX_RETRIES", raising=False)
    c = OllamaLLMClient(model="m", base_url="http://localhost:11434", rate_limit_max_retries=0)
    max_retries, initial, cap = c._rate_limit_retry_config()
    assert max_retries == 0 and initial > 0 and cap >= initial
    # No override → env schedule (default 3).
    c2 = OllamaLLMClient(model="m", base_url="http://localhost:11434")
    assert c2._rate_limit_retry_config()[0] == 3


def test_ollama_override_negative_clamped_to_zero():
    """A negative rate_limit_max_retries override clamps to 0, not a negative retry budget."""
    c = OllamaLLMClient(model="m", base_url="http://localhost:11434", rate_limit_max_retries=-3)
    assert c._rate_limit_max_retries_override == 0


def test_claude_rate_limit_override_stored():
    """ClaudeLLMClient stores an explicit override; omitting it leaves None (env schedule)."""
    c = ClaudeLLMClient(model="claude-opus-4-8", api_key="k", rate_limit_max_retries=0)
    assert c._rate_limit_max_retries_override == 0
    c2 = ClaudeLLMClient(model="claude-opus-4-8", api_key="k")
    assert c2._rate_limit_max_retries_override is None
