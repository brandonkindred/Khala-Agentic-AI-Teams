"""Tests for get_client under the provider-list-only contract: dummy override,
the failover wrapper returned for a configured list, per-agent model defaults, the
agent-key attribution wrapper, and the not-configured error when the list is empty.

The Postgres-backed provider list is the sole source of LLM resolution (``dummy``
aside), so these tests seed a one-entry list and assert behavior through the
:class:`FailoverLLMClient` the factory returns. The concrete provider client the
failover wrapper dispatches to is the shared cached singleton (``_ollama_cached``),
so attribution spies patch that cached client's ``_complete_json_impl`` seam.
"""

import pytest

from llm_service import (
    DummyLLMClient,
    LLMClient,
    LLMNotConfiguredError,
    OllamaLLMClient,
    attributed_client,
    client_agent_key,
    get_client,
    unwrap_client,
)
from llm_service import provider_store as ps
from llm_service.attribution import current_attribution, llm_attribution
from llm_service.factory import FailoverLLMClient, _AttributingClient, _ollama_cached


def _ollama_entry(entry_id=1, *, model="", base_url="http://127.0.0.1:11434", api_key=""):
    return ps.ProviderEntry(
        id=entry_id,
        label="e",
        provider="ollama",
        model=model,
        base_url=base_url,
        api_key=api_key,
        sort_order=entry_id,
        limit_exceeded=False,
        limit_type="",
        reset_at=None,
    )


@pytest.fixture
def seed_ollama(monkeypatch):
    """Seed a one-entry Ollama provider list so get_client resolves to failover.

    Returns a helper that installs a list of one Ollama entry with the given model /
    base URL and returns that entry, so a test can control the resolved defaults.
    """

    def _seed(model="cfg-model", base_url="http://127.0.0.1:11434"):
        entry = _ollama_entry(model=model, base_url=base_url)
        monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [entry])
        monkeypatch.setattr(ps, "select_active_entry", lambda es, **k: es[0])
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        return entry

    return _seed


def test_get_client_dummy_when_provider_dummy(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    c = get_client("soc2")
    # Dummy is always returned unwrapped (it doubles as a Strands Model).
    assert isinstance(c, DummyLLMClient)


def test_get_client_dummy_ignores_on_reasoning(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    c = get_client("soc2", on_reasoning=lambda _t: None)
    assert isinstance(c, DummyLLMClient)


def test_get_client_raises_when_no_list_and_not_dummy(monkeypatch):
    """The provider list is the sole source: empty list + non-dummy provider raises."""
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [])
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    with pytest.raises(LLMNotConfiguredError):
        get_client("backend")


def test_keyed_client_wraps_failover_and_is_llmclient(seed_ollama):
    """A keyed client is an _AttributingClient over a FailoverLLMClient, and it
    passes isinstance(c, LLMClient) so interface-branching resolvers keep it."""
    seed_ollama(model="m")
    c = get_client("backend")
    assert isinstance(c, _AttributingClient)
    assert isinstance(c._inner, FailoverLLMClient)
    assert isinstance(c, LLMClient)
    # Delegation still works (virtual registration adds no shadowing methods).
    assert c.get_max_context_tokens() == c._inner.get_max_context_tokens()
    assert c.model == "m"  # delegated through the failover wrapper to the active client


def test_attributing_client_supports_structured_output_passthrough(seed_ollama):
    """supports_structured_output has no explicit wrapper method — it must reach the
    inner client via the same __getattr__ delegation as get_max_context_tokens above."""
    seed_ollama(model="m")
    c = get_client("backend")
    assert c.supports_structured_output() == c._inner.supports_structured_output()
    assert c.supports_structured_output() is True  # Ollama


def test_get_client_none_is_unwrapped_failover(seed_ollama):
    seed_ollama(model="default-model")
    c = get_client(None)
    # No agent_key → nothing to bind → the bare failover client is returned.
    assert isinstance(c, FailoverLLMClient)
    assert not isinstance(c, _AttributingClient)
    assert c.model == "default-model"


def test_failover_reports_smallest_candidate_context() -> None:
    """Prompt sizing must account for every provider a generation may use."""
    entries = [object(), object()]
    contexts = {entries[0]: 32_000, entries[1]: 8_000}
    builds: list[tuple] = []

    class _Client:
        def __init__(self, context_tokens):
            self.context_tokens = context_tokens

        def get_max_context_tokens(self):
            return self.context_tokens

    def _build(entry, retry_override, model_override):
        builds.append((entry, retry_override, model_override))
        return _Client(contexts[entry])

    client = FailoverLLMClient(
        lambda: entries,
        _build,
        lambda _entry, _error: None,
        model_override="planning-model",
    )

    assert client.get_min_context_tokens() == 8_000
    assert builds == [
        (entries[0], None, "planning-model"),
        (entries[1], None, "planning-model"),
    ]


def test_get_client_per_agent_model_default(seed_ollama, monkeypatch):
    """A blank entry model resolves per-agent via the shared resolver defaults."""
    seed_ollama(model="")  # blank → resolver default applies
    monkeypatch.setenv("LLM_MODEL", "global")
    monkeypatch.setenv("LLM_MODEL_backend", "backend-model")
    assert get_client("backend").model == "backend-model"
    assert get_client(None).model == "global"


def test_get_client_caching_shares_concrete_singleton(seed_ollama):
    """Each get_client returns a fresh failover wrapper, but the concrete provider
    client it dispatches to is the shared cached singleton (_ollama_cached)."""
    seed_ollama(model="cached-model")
    c1, c2 = get_client("backend"), get_client("backend")
    # Fresh wrappers each call...
    assert c1 is not c2
    # ...but the underlying concrete Ollama client is the shared cached singleton.
    concrete, _ = _ollama_cached("cached-model", "http://127.0.0.1:11434", 900.0, None, "")
    assert isinstance(concrete, OllamaLLMClient)
    assert c1.get_max_context_tokens() == concrete.get_max_context_tokens()


def test_wrapper_binds_agent_key_into_attribution(seed_ollama, monkeypatch):
    """The wrapper binds its agent_key onto the attribution context for the call."""
    seed_ollama(model="m")
    # The failover wrapper dispatches to the cached concrete client; patch that
    # client's _impl seam so the real public complete_json (which binds objective)
    # and the factory wrapper (which binds agent_key) both run.
    concrete, _ = _ollama_cached("m", "http://127.0.0.1:11434", 900.0, None, "")
    captured: dict = {}

    def spy(prompt, **kwargs):
        attr = current_attribution()
        captured["agent_key"] = attr.agent_key
        captured["objective"] = attr.objective
        return {"ok": True}

    monkeypatch.setattr(concrete, "_complete_json_impl", spy)
    c = get_client("ranker")
    out = c.complete_json("p", objective="rank candidates")
    assert out == {"ok": True}
    assert captured["agent_key"] == "ranker"
    assert captured["objective"] == "rank candidates"
    assert current_attribution().agent_key == ""  # restored after the call


def test_get_client_empty_agent_key_returns_unwrapped(seed_ollama, monkeypatch):
    """A falsy ("") agent_key binds nothing — the bare failover client is returned
    (matching the None case), so a call under it never clobbers an enclosing
    orchestrator's agent_key with an empty string."""
    seed_ollama(model="m")
    c = get_client("")
    assert not isinstance(c, _AttributingClient)
    assert isinstance(c, FailoverLLMClient)

    concrete, _ = _ollama_cached("m", "http://127.0.0.1:11434", 900.0, None, "")
    captured: dict = {}

    def spy(prompt, **kwargs):
        captured["agent_key"] = current_attribution().agent_key
        return {"ok": True}

    monkeypatch.setattr(concrete, "_complete_json_impl", spy)
    with llm_attribution(agent_key="orchestrator"):
        c.complete_json("p", objective="x")
    # The outer agent_key survives — the empty-key client did not override it.
    assert captured["agent_key"] == "orchestrator"


def test_wrapper_does_not_clobber_outer_team_objective(seed_ollama, monkeypatch):
    """An enclosing orchestrator team/objective survives the wrapper's agent_key bind."""
    seed_ollama(model="m")
    concrete, _ = _ollama_cached("m", "http://127.0.0.1:11434", 900.0, None, "")
    captured: dict = {}

    def spy(prompt, **kwargs):
        attr = current_attribution()
        captured["team"] = attr.team
        captured["agent_key"] = attr.agent_key
        return {}

    monkeypatch.setattr(concrete, "_complete_json_impl", spy)
    c = get_client("ranker")
    with llm_attribution(team="job_matching", objective="match jobs"):
        c.complete_json("p", objective="rank candidates")
    assert captured["team"] == "job_matching"
    assert captured["agent_key"] == "ranker"


def test_client_agent_key_and_attributed_client(seed_ollama):
    """A reconstructed client can re-apply the original client's agent attribution."""
    seed_ollama(model="m")
    keyed = get_client("blog")
    assert client_agent_key(keyed) == "blog"

    raw = unwrap_client(keyed)  # stops at the failover layer (Strands routing guard)
    assert isinstance(raw, FailoverLLMClient)
    assert client_agent_key(raw) is None  # an unwrapped client carries no identity

    # A fresh override client (e.g. a per-model override) can preserve the identity.
    override = OllamaLLMClient(model="override-model")
    rewrapped = attributed_client(override, client_agent_key(keyed))
    assert isinstance(rewrapped, _AttributingClient)
    assert client_agent_key(rewrapped) == "blog"
    assert rewrapped.model == "override-model"

    # No key → returned unchanged (no wrapper).
    assert attributed_client(override, None) is override
