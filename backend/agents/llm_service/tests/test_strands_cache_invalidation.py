"""Strands model-cache invalidation on API-key rotation.

The Strands model cache key includes the active provider's API-key fingerprint so
an in-place key rotation rebuilds the adapter even in containers that pick the new
key up only via the runtime-config TTL — they never call ``clear_model_cache``
(which fires solely in the PUT handler's process). Without the fingerprint a
rotated key would keep being served by a model wrapping a client built with the
old key until the process restarts.

These tests live in a dedicated module so the cache-invalidation coverage is
collectable on its own.
"""

from __future__ import annotations

import pytest

import llm_service.config as cfg
import llm_service.strands_provider as sp
from llm_service.strands_provider import (
    _active_provider_key_fingerprint,
    _clear_strands_model_cache_for_testing,
    get_strands_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Empty the Strands model cache before and after each test."""
    _clear_strands_model_cache_for_testing()
    yield
    _clear_strands_model_cache_for_testing()


def test_fingerprint_is_provider_specific(monkeypatch):
    """The key fingerprint is provider-specific: one provider's key never sets the other's."""
    # The Claude path reads the Claude key; the Ollama path reads the Ollama key —
    # so one provider's key never determines the other's cache identity.
    monkeypatch.setattr(cfg, "resolve_provider", lambda: "claude")
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "sk-claude-123")
    monkeypatch.setattr(cfg, "resolve_ollama_api_key", lambda: "ollama-key-should-not-leak")
    claude_fp = _active_provider_key_fingerprint()

    monkeypatch.setattr(cfg, "resolve_provider", lambda: "ollama")
    ollama_fp = _active_provider_key_fingerprint()

    assert claude_fp not in ("", "no-key")
    assert ollama_fp not in ("", "no-key")
    assert claude_fp != ollama_fp


def test_fingerprint_rotates_with_key(monkeypatch):
    """Rotating the API key changes the computed fingerprint."""
    monkeypatch.setattr(cfg, "resolve_provider", lambda: "claude")
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "old-key")
    old = _active_provider_key_fingerprint()
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "new-key")
    assert _active_provider_key_fingerprint() != old


def test_fingerprint_no_key(monkeypatch):
    """An absent key yields the sentinel fingerprint 'no-key'."""
    monkeypatch.setattr(cfg, "resolve_provider", lambda: "claude")
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "")
    assert _active_provider_key_fingerprint() == "no-key"


def test_key_rotation_rebuilds_cached_model(monkeypatch):
    """A rotated key fingerprint rebuilds the cached Strands model; same fingerprint hits cache."""
    # Same fingerprint -> cache hit; rotated fingerprint -> rebuild. This is what
    # lets a TTL-refreshed key reach agents without a process restart. get_client
    # and LLMClientModel are stubbed so no real provider client is constructed.
    monkeypatch.setattr(cfg, "resolve_base_url", lambda: "http://host")
    monkeypatch.setattr(cfg, "resolve_model_for_provider", lambda ak, provider=None: "model-x")
    monkeypatch.setattr(sp, "get_client", lambda ak: object())
    monkeypatch.setattr(sp, "LLMClientModel", lambda *a, **k: object())

    fingerprints = iter(["fp-old", "fp-old", "fp-new"])
    monkeypatch.setattr(sp, "_active_provider_key_fingerprint", lambda *_a: next(fingerprints))

    m1 = get_strands_model()  # fp-old -> build
    m2 = get_strands_model()  # fp-old -> cache hit
    m3 = get_strands_model()  # fp-new -> rebuild
    assert m1 is m2
    assert m1 is not m3


def test_provider_switch_rebuilds_cached_model(monkeypatch):
    """A provider switch rebuilds the model even when model_id/base_url/fingerprint coincide."""
    # A provider switch must rebuild the adapter even when model_id, base_url, and
    # the key fingerprint all coincide (e.g. two keyless providers that resolve the
    # same model_id) — otherwise a model wrapping the wrong provider's client would
    # be served. The active provider is part of the cache key to guarantee this.
    monkeypatch.setattr(cfg, "resolve_base_url", lambda: "http://host")
    monkeypatch.setattr(cfg, "resolve_model_for_provider", lambda ak, provider=None: "model-x")
    monkeypatch.setattr(sp, "_active_provider_key_fingerprint", lambda *_a: "no-key")
    monkeypatch.setattr(sp, "get_client", lambda ak: object())
    monkeypatch.setattr(sp, "LLMClientModel", lambda *a, **k: object())

    providers = iter(["ollama", "ollama", "dummy"])
    monkeypatch.setattr(cfg, "resolve_provider", lambda: next(providers))

    m1 = get_strands_model()  # ollama -> build
    m2 = get_strands_model()  # ollama -> cache hit (identical key)
    m3 = get_strands_model()  # dummy: same model_id/base_url/fingerprint -> rebuild
    assert m1 is m2
    assert m1 is not m3


def test_get_strands_model_resolves_provider_once(monkeypatch):
    """get_strands_model resolves the provider exactly once per call."""
    # The hot path must resolve the provider a single time per call and thread it
    # into the model-id + fingerprint helpers (it took the runtime lock 3x before).
    calls = {"n": 0}

    def _counting_provider():
        calls["n"] += 1
        return "ollama"

    monkeypatch.setattr(cfg, "resolve_provider", _counting_provider)
    monkeypatch.setattr(cfg, "resolve_base_url", lambda: "http://host")
    monkeypatch.setattr(cfg, "resolve_model", lambda ak=None: "model-x")
    monkeypatch.setattr(cfg, "resolve_ollama_api_key", lambda: "")
    monkeypatch.setattr(sp, "get_client", lambda ak: object())
    monkeypatch.setattr(sp, "LLMClientModel", lambda *a, **k: object())

    get_strands_model()
    assert calls["n"] == 1


def test_provider_threads_into_helpers_without_extra_resolve(monkeypatch):
    """An explicitly-passed provider is honored by the helpers without re-resolving."""
    # resolve_model_for_provider / _active_provider_key_fingerprint must honor an
    # explicitly-passed provider and NOT re-resolve it.
    monkeypatch.setattr(cfg, "resolve_provider", lambda: pytest.fail("should not re-resolve"))
    monkeypatch.setattr(cfg, "resolve_claude_model", lambda ak=None: "claude-x")
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "sk-abc")
    assert cfg.resolve_model_for_provider(None, provider="claude") == "claude-x"
    fp = _active_provider_key_fingerprint("claude")
    assert fp not in ("", "no-key")
