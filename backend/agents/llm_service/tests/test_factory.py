"""Tests for get_client: dummy vs ollama, caching, per-agent override."""

import pytest

from llm_service import DummyLLMClient, OllamaLLMClient, get_client


def test_get_client_dummy_when_provider_dummy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    c = get_client("soc2")
    assert isinstance(c, DummyLLMClient)


def test_get_client_ollama_when_provider_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    c = get_client("soc2")
    assert isinstance(c, OllamaLLMClient)
    assert c.model == "test-model"


def test_get_client_caching_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "cached-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    c1 = get_client("backend")
    c2 = get_client("backend")
    assert c1 is c2
    assert c1.model == "cached-model"


def test_get_client_per_agent_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "global")
    monkeypatch.setenv("LLM_MODEL_backend", "backend-model")
    c_global = get_client(None)
    c_backend = get_client("backend")
    assert c_backend.model == "backend-model"
    assert c_global.model == "global"


def test_get_client_none_uses_global_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "default-model")
    c = get_client(None)
    assert isinstance(c, OllamaLLMClient)
    assert c.model == "default-model"


def test_get_client_with_on_reasoning_is_uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reasoning callback yields a fresh, uncached client carrying the hook."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "rx-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    cb = lambda _t: None  # noqa: E731
    c1 = get_client("backend", on_reasoning=cb)
    c2 = get_client("backend", on_reasoning=cb)
    cached = get_client("backend")  # no hook → shared singleton
    assert isinstance(c1, OllamaLLMClient)
    assert c1.on_reasoning is cb
    assert c1 is not c2  # each callback-bearing client is distinct (uncached)
    assert c1 is not cached and c2 is not cached
    assert cached.on_reasoning is None


def test_get_client_dummy_ignores_on_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    c = get_client("soc2", on_reasoning=lambda _t: None)
    assert isinstance(c, DummyLLMClient)
