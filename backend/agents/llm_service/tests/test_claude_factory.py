"""Tests for the Claude provider path in get_client (factory)."""

from __future__ import annotations

import pytest

from llm_service import ClaudeLLMClient, clear_client_cache, get_client
from llm_service.factory import _AttributingClient


@pytest.fixture(autouse=True)
def _claude_env(monkeypatch: pytest.MonkeyPatch):
    """Select the Claude provider with a test key and clear caches around each test."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    clear_client_cache()
    yield
    clear_client_cache()


def test_get_client_returns_claude_unwrapped_for_no_agent():
    """get_client(None) returns the bare ClaudeLLMClient at the default model."""
    c = get_client(None)
    assert isinstance(c, ClaudeLLMClient)
    assert c.model == "claude-opus-4-8"


def test_get_client_wraps_keyed_claude_client():
    """A keyed get_client wraps the Claude client in an _AttributingClient."""
    c = get_client("backend")
    assert isinstance(c, _AttributingClient)
    assert isinstance(c._inner, ClaudeLLMClient)


def test_get_client_anthropic_alias_provider(monkeypatch):
    """LLM_PROVIDER=anthropic also yields a ClaudeLLMClient."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    clear_client_cache()
    c = get_client(None)
    assert isinstance(c, ClaudeLLMClient)


def test_claude_cache_keyed_by_model_and_key(monkeypatch):
    """The Claude cache returns a singleton per (model, key); a change rebuilds it."""
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
    """clear_client_cache() forces the next get_client to rebuild the Claude client."""
    c1 = get_client(None)
    clear_client_cache()
    c2 = get_client(None)
    assert c1 is not c2


def test_on_reasoning_returns_fresh_uncached_claude_client():
    """An on_reasoning sink yields a fresh, uncached Claude client carrying the callback."""

    def cb(_s: str) -> None:
        return None

    c1 = get_client(None, on_reasoning=cb)
    c2 = get_client(None, on_reasoning=cb)
    assert isinstance(c1, ClaudeLLMClient)
    assert c1 is not c2  # per-caller callback must never be cached
    assert c1.on_reasoning is cb


def test_clear_client_cache_also_clears_strands_cache(monkeypatch):
    """clear_client_cache() invalidates the Strands model cache (public behavior).

    Asserts the public contract — a cached model is served again on a second call,
    and clear_client_cache() forces the next call to rebuild — instead of inspecting
    the private ``_model_cache`` dict. get_client/LLMClientModel are stubbed so no
    real provider client is constructed (mirrors the strands cache suites).
    """
    import llm_service.config as cfg
    import llm_service.strands_provider as sp
    from llm_service.strands_provider import get_strands_model

    monkeypatch.setattr(cfg, "resolve_provider", lambda: "ollama")
    monkeypatch.setattr(cfg, "resolve_base_url", lambda: "http://host")
    monkeypatch.setattr(cfg, "resolve_model_for_provider", lambda ak, provider=None: "model-x")
    monkeypatch.setattr(cfg, "resolve_ollama_api_key", lambda: "")
    monkeypatch.setattr(sp, "_active_provider_key_fingerprint", lambda *_a: "no-key")
    monkeypatch.setattr(sp, "get_client", lambda ak: object())
    monkeypatch.setattr(sp, "LLMClientModel", lambda *a, **k: object())

    m1 = get_strands_model()  # build + cache
    m2 = get_strands_model()  # identical key -> cache hit
    assert m1 is m2

    clear_client_cache()  # must invalidate the strands cache too

    m3 = get_strands_model()  # cache emptied -> rebuild
    assert m1 is not m3
