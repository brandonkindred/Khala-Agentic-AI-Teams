"""Tests for the Claude provider path in get_client under the provider-list-only
contract: a seeded Claude entry resolves through the failover wrapper, the concrete
Claude client is cached (and cleared) via the shared cache, and on_reasoning yields a
fresh uncached client. The Claude entry carries its own key (no env fallback)."""

from __future__ import annotations

import pytest

from llm_service import ClaudeLLMClient, clear_client_cache, get_client
from llm_service import provider_store as ps
from llm_service.factory import FailoverLLMClient, _AttributingClient, _claude_cached


def _claude_entry(entry_id=1, *, model="claude-opus-4-8", api_key="sk-test-key"):
    return ps.ProviderEntry(
        id=entry_id,
        label="e",
        provider="claude",
        model=model,
        base_url="",
        api_key=api_key,
        sort_order=entry_id,
        limit_exceeded=False,
        limit_type="",
        reset_at=None,
    )


@pytest.fixture
def seed_claude(monkeypatch):
    """Seed a one-entry Claude provider list so get_client resolves to failover."""

    def _seed(model="claude-opus-4-8", api_key="sk-test-key"):
        entry = _claude_entry(model=model, api_key=api_key)
        monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [entry])
        monkeypatch.setattr(ps, "select_active_entry", lambda es, **k: es[0])
        monkeypatch.setenv("LLM_PROVIDER", "claude")
        clear_client_cache()
        return entry

    return _seed


def test_get_client_returns_failover_unwrapped_for_no_agent(seed_claude):
    """get_client(None) returns the bare failover client; .model delegates to Claude."""
    seed_claude(model="claude-opus-4-8")
    c = get_client(None)
    assert isinstance(c, FailoverLLMClient)
    assert c.model == "claude-opus-4-8"


def test_get_client_wraps_keyed_claude_client(seed_claude):
    """A keyed get_client wraps the failover client in an _AttributingClient."""
    seed_claude()
    c = get_client("backend")
    assert isinstance(c, _AttributingClient)
    assert isinstance(c._inner, FailoverLLMClient)


def test_claude_entry_uses_its_own_key_no_env_fallback(seed_claude, monkeypatch):
    """The concrete Claude client authenticates with the entry's key, never env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-should-not-be-used")
    seed_claude(model="claude-opus-4-8", api_key="sk-entry")
    concrete, _ = _claude_cached("claude-opus-4-8", "sk-entry", 900.0, None)
    assert isinstance(concrete, ClaudeLLMClient)
    assert concrete.api_key == "sk-entry"


def test_claude_cache_keyed_by_model_and_key():
    """The Claude cache returns a singleton per (model, key); a change rebuilds it."""
    c1, _ = _claude_cached("claude-opus-4-8", "sk-a", 900.0, None)
    c2, _ = _claude_cached("claude-opus-4-8", "sk-a", 900.0, None)
    assert c1 is c2  # same model + key -> cached singleton
    c3, _ = _claude_cached("claude-opus-4-8", "sk-b", 900.0, None)
    assert c3 is not c1  # different key -> fresh client
    c4, _ = _claude_cached("claude-sonnet-4-6", "sk-a", 900.0, None)
    assert c4 is not c1 and c4.model == "claude-sonnet-4-6"  # different model -> fresh


def test_clear_client_cache_drops_claude(seed_claude):
    """clear_client_cache() forces the next dispatch to rebuild the Claude client."""
    seed_claude()
    concrete1, _ = _claude_cached("claude-opus-4-8", "sk-test-key", 900.0, None)
    clear_client_cache()
    concrete2, _ = _claude_cached("claude-opus-4-8", "sk-test-key", 900.0, None)
    assert concrete1 is not concrete2


def test_on_reasoning_returns_fresh_uncached_claude_client(seed_claude):
    """An on_reasoning sink yields a fresh, uncached Claude client carrying the callback."""
    seed_claude()

    def cb(_s: str) -> None:
        return None

    c1 = get_client(None, on_reasoning=cb)
    # Delegated attribute access builds a fresh (uncached) Claude client with the hook.
    assert c1.on_reasoning is cb
    assert isinstance(c1, FailoverLLMClient)


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
