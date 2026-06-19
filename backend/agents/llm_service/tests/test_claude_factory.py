"""Tests for the Claude provider path in get_client (factory)."""

from __future__ import annotations

import pytest

from llm_service import ClaudeLLMClient, clear_client_cache, get_client
from llm_service.factory import _AttributingClient


@pytest.fixture(autouse=True)
def _claude_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    clear_client_cache()
    yield
    clear_client_cache()


def test_get_client_returns_claude_unwrapped_for_no_agent():
    c = get_client(None)
    assert isinstance(c, ClaudeLLMClient)
    assert c.model == "claude-opus-4-8"


def test_get_client_wraps_keyed_claude_client():
    c = get_client("backend")
    assert isinstance(c, _AttributingClient)
    assert isinstance(c._inner, ClaudeLLMClient)


def test_get_client_anthropic_alias_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    clear_client_cache()
    c = get_client(None)
    assert isinstance(c, ClaudeLLMClient)


def test_claude_cache_keyed_by_model_and_key(monkeypatch):
    c1 = get_client(None)
    c2 = get_client(None)
    assert c1 is c2  # same model + key -> cached singleton

    # Changing the key yields a fresh client.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-different-key")
    c3 = get_client(None)
    assert c3 is not c1

    # Changing the model yields a fresh client.
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    c4 = get_client(None)
    assert c4 is not c3
    assert c4.model == "claude-sonnet-4-6"


def test_clear_client_cache_drops_claude(monkeypatch):
    c1 = get_client(None)
    clear_client_cache()
    c2 = get_client(None)
    assert c1 is not c2


def test_on_reasoning_returns_fresh_uncached_claude_client():
    def cb(_s: str) -> None:
        return None

    c1 = get_client(None, on_reasoning=cb)
    c2 = get_client(None, on_reasoning=cb)
    assert isinstance(c1, ClaudeLLMClient)
    assert c1 is not c2  # per-caller callback must never be cached
    assert c1.on_reasoning is cb


def test_clear_client_cache_also_clears_strands_cache():
    from llm_service import strands_provider

    strands_provider._model_cache[("m", "u", "json", None)] = object()
    clear_client_cache()
    assert strands_provider._model_cache == {}
