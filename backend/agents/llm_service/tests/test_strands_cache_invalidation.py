"""Strands model-cache invalidation on API-key rotation.

The Strands model cache key includes the active provider's API-key fingerprint so
an in-place key rotation rebuilds the adapter even in containers that pick the new
key up only via the runtime-config TTL — they never call ``clear_model_cache``
(which fires solely in the PUT handler's process). Without the fingerprint a
rotated key would keep being served by a model wrapping a client built with the
old key until the process restarts.

These tests live in a dedicated module (not ``test_strands_provider.py``, which is
a stale suite that predates the current ``LLMClientModel`` implementation) so the
new coverage is collectable on its own.
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
    _clear_strands_model_cache_for_testing()
    yield
    _clear_strands_model_cache_for_testing()


def test_fingerprint_is_provider_specific(monkeypatch):
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
    monkeypatch.setattr(cfg, "resolve_provider", lambda: "claude")
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "old-key")
    old = _active_provider_key_fingerprint()
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "new-key")
    assert _active_provider_key_fingerprint() != old


def test_fingerprint_no_key(monkeypatch):
    monkeypatch.setattr(cfg, "resolve_provider", lambda: "claude")
    monkeypatch.setattr(cfg, "resolve_claude_api_key", lambda: "")
    assert _active_provider_key_fingerprint() == "no-key"


def test_key_rotation_rebuilds_cached_model(monkeypatch):
    # Same fingerprint -> cache hit; rotated fingerprint -> rebuild. This is what
    # lets a TTL-refreshed key reach agents without a process restart. get_client
    # and LLMClientModel are stubbed so no real provider client is constructed.
    monkeypatch.setattr(cfg, "resolve_base_url", lambda: "http://host")
    monkeypatch.setattr(cfg, "resolve_model_for_provider", lambda ak: "model-x")
    monkeypatch.setattr(sp, "get_client", lambda ak: object())
    monkeypatch.setattr(sp, "LLMClientModel", lambda *a, **k: object())

    fingerprints = iter(["fp-old", "fp-old", "fp-new"])
    monkeypatch.setattr(sp, "_active_provider_key_fingerprint", lambda: next(fingerprints))

    m1 = get_strands_model()  # fp-old -> build
    m2 = get_strands_model()  # fp-old -> cache hit
    m3 = get_strands_model()  # fp-new -> rebuild
    assert m1 is m2
    assert m1 is not m3


def test_provider_switch_rebuilds_cached_model(monkeypatch):
    # A provider switch must rebuild the adapter even when model_id, base_url, and
    # the key fingerprint all coincide (e.g. two keyless providers that resolve the
    # same model_id) — otherwise a model wrapping the wrong provider's client would
    # be served. The active provider is part of the cache key to guarantee this.
    monkeypatch.setattr(cfg, "resolve_base_url", lambda: "http://host")
    monkeypatch.setattr(cfg, "resolve_model_for_provider", lambda ak: "model-x")
    monkeypatch.setattr(sp, "_active_provider_key_fingerprint", lambda: "no-key")
    monkeypatch.setattr(sp, "get_client", lambda ak: object())
    monkeypatch.setattr(sp, "LLMClientModel", lambda *a, **k: object())

    providers = iter(["ollama", "ollama", "dummy"])
    monkeypatch.setattr(cfg, "resolve_provider", lambda: next(providers))

    m1 = get_strands_model()  # ollama -> build
    m2 = get_strands_model()  # ollama -> cache hit (identical key)
    m3 = get_strands_model()  # dummy: same model_id/base_url/fingerprint -> rebuild
    assert m1 is m2
    assert m1 is not m3
