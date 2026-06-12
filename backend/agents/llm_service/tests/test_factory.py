"""Tests for get_client: dummy vs ollama, caching, per-agent override, and the
agent-key attribution wrapper returned for keyed clients."""

import pytest

from llm_service import (
    DummyLLMClient,
    LLMClient,
    OllamaLLMClient,
    attributed_client,
    client_agent_key,
    get_client,
    unwrap_client,
)
from llm_service.attribution import current_attribution
from llm_service.factory import _AttributingClient


def test_wrapper_is_an_llmclient_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper must pass ``isinstance(c, LLMClient)`` so resolvers that branch
    on the interface (e.g. SE's resolve_strands_model) don't drop keyed clients."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "m")
    c = get_client("backend")
    assert isinstance(c, _AttributingClient)
    assert isinstance(c, LLMClient)
    # Delegation still works (virtual registration adds no shadowing methods).
    assert c.get_max_context_tokens() == c._inner.get_max_context_tokens()


def test_get_client_dummy_when_provider_dummy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    c = get_client("soc2")
    # Dummy is always returned unwrapped (it doubles as a Strands Model).
    assert isinstance(c, DummyLLMClient)


def test_get_client_ollama_when_provider_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    c = get_client("soc2")
    # A keyed client is wrapped for attribution; the underlying client is Ollama.
    assert isinstance(c, _AttributingClient)
    assert isinstance(c._inner, OllamaLLMClient)
    assert c.model == "test-model"  # delegated via __getattr__


def test_get_client_caching_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "cached-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    c1 = get_client("backend")
    c2 = get_client("backend")
    # Each call returns a fresh lightweight wrapper, but the underlying client
    # is the shared cached singleton.
    assert c1._inner is c2._inner
    assert c1.model == "cached-model"


def test_get_client_per_agent_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "global")
    monkeypatch.setenv("LLM_MODEL_backend", "backend-model")
    c_global = get_client(None)
    c_backend = get_client("backend")
    assert c_backend.model == "backend-model"
    assert c_global.model == "global"


def test_get_client_none_is_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "default-model")
    c = get_client(None)
    # No agent_key → nothing to bind → the raw cached client is returned.
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
    cached = get_client("backend")  # no hook → shared singleton (still wrapped)
    assert isinstance(c1, _AttributingClient)
    assert c1.on_reasoning is cb  # delegated via __getattr__
    assert c1._inner is not c2._inner  # each callback-bearing client is distinct (uncached)
    assert c1._inner is not cached._inner and c2._inner is not cached._inner
    assert cached.on_reasoning is None


def test_get_client_dummy_ignores_on_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    c = get_client("soc2", on_reasoning=lambda _t: None)
    assert isinstance(c, DummyLLMClient)


def test_wrapper_binds_agent_key_into_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper binds its agent_key onto the attribution context for the call."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "m")
    c = get_client("ranker")
    captured: dict = {}

    def spy(prompt: str, **kwargs: object) -> dict:
        attr = current_attribution()
        captured["agent_key"] = attr.agent_key
        captured["objective"] = attr.objective
        return {"ok": True}

    # Patch the inner implementation so the real public complete_json (which binds
    # the objective) and the factory wrapper (which binds agent_key) both run.
    monkeypatch.setattr(c._inner, "_complete_json_impl", spy)
    out = c.complete_json("p", objective="rank candidates")
    assert out == {"ok": True}
    assert captured["agent_key"] == "ranker"
    assert captured["objective"] == "rank candidates"
    # Attribution is restored after the call.
    assert current_attribution().agent_key == ""


def test_client_agent_key_and_attributed_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconstructed client can re-apply the original client's agent attribution."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "m")
    keyed = get_client("blog")
    assert client_agent_key(keyed) == "blog"

    raw = unwrap_client(keyed)
    assert client_agent_key(raw) is None  # an unwrapped client carries no identity

    # A fresh override client (e.g. a per-model override) can preserve the identity.
    override = OllamaLLMClient(model="override-model")
    rewrapped = attributed_client(override, client_agent_key(keyed))
    assert isinstance(rewrapped, _AttributingClient)
    assert client_agent_key(rewrapped) == "blog"
    assert rewrapped.model == "override-model"

    # No key → returned unchanged (no wrapper).
    assert attributed_client(override, None) is override


def test_wrapper_does_not_clobber_outer_team_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    """An enclosing orchestrator team/objective survives the wrapper's agent_key bind."""
    from llm_service import llm_attribution

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "m")
    c = get_client("ranker")
    captured: dict = {}

    def spy(prompt: str, **kwargs: object) -> dict:
        attr = current_attribution()
        captured["team"] = attr.team
        captured["agent_key"] = attr.agent_key
        return {}

    monkeypatch.setattr(c._inner, "_complete_json_impl", spy)
    with llm_attribution(team="job_matching", objective="match jobs"):
        c.complete_json("p", objective="rank candidates")
    assert captured["team"] == "job_matching"
    assert captured["agent_key"] == "ranker"
