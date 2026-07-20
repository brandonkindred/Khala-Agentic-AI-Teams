"""Tests for shared_neo4j — all faked, no live Neo4j or graphiti_core required.

Mirrors ``shared_postgres``'s test idiom: prove the env gate, the config
resolvers, the lazy/locked singleton lifecycle (via a monkeypatched builder), the
gated-off schema registration, and the timing decorator — none of which need a
database or the ``graphiti_core`` dependency installed.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from shared_neo4j import (
    GRAPH_SCHEMA,
    GraphSchema,
    GraphUnavailable,
    config,
    is_neo4j_enabled,
    register_graph_indices,
    timed_graph_op,
)
from shared_neo4j import client as client_mod


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test starts/ends with no cached Graphiti client."""
    client_mod._graphiti = None
    yield
    client_mod._graphiti = None


# ---------------------------------------------------------------------------
# Enablement gate
# ---------------------------------------------------------------------------
def test_is_neo4j_enabled_false_when_unset(monkeypatch):
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)
    assert is_neo4j_enabled() is False


def test_is_neo4j_enabled_false_when_blank(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "   ")
    assert is_neo4j_enabled() is False


def test_is_neo4j_enabled_true_when_set(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    assert is_neo4j_enabled() is True


# ---------------------------------------------------------------------------
# Config resolvers
# ---------------------------------------------------------------------------
def test_bolt_url_requires_gate(monkeypatch):
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)
    with pytest.raises(AssertionError):
        config.neo4j_bolt_url()


def test_connection_defaults(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    monkeypatch.delenv("NEO4J_USER", raising=False)
    monkeypatch.delenv("NEO4J_DATABASE", raising=False)
    assert config.neo4j_bolt_url() == "bolt://neo4j:7687"
    assert config.neo4j_user() == "neo4j"
    assert config.neo4j_database() == "neo4j"


def test_connection_overrides(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://example:7687")
    monkeypatch.setenv("NEO4J_USER", "graph")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_DATABASE", "kg")
    assert config.neo4j_user() == "graph"
    assert config.neo4j_password() == "secret"
    assert config.neo4j_database() == "kg"


def test_graphiti_llm_model_override_wins(monkeypatch):
    monkeypatch.setenv("GRAPHITI_LLM_MODEL", "custom-model:cloud")
    assert config.graphiti_llm_model() == "custom-model:cloud"


def test_graphiti_llm_model_falls_back_to_cognition(monkeypatch):
    monkeypatch.delenv("GRAPHITI_LLM_MODEL", raising=False)
    # Resolves through llm_service; just assert it returns a non-empty model id.
    assert config.graphiti_llm_model().strip()


def test_embed_model_and_dim_defaults(monkeypatch):
    monkeypatch.delenv("GRAPHITI_EMBED_MODEL", raising=False)
    monkeypatch.delenv("GRAPHITI_EMBED_DIM", raising=False)
    assert config.graphiti_embed_model() == "nomic-embed-text"
    assert config.graphiti_embed_dim() == 768


@pytest.mark.parametrize("raw", ["not-an-int", "0", "-5", ""])
def test_embed_dim_garbage_falls_back(monkeypatch, raw):
    monkeypatch.setenv("GRAPHITI_EMBED_DIM", raw)
    assert config.graphiti_embed_dim() == 768


def test_embed_dim_valid_override(monkeypatch):
    monkeypatch.setenv("GRAPHITI_EMBED_DIM", "1024")
    assert config.graphiti_embed_dim() == 1024


def test_openai_compatible_base_url_appends_v1(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://ollama.example.com/")
    assert config.openai_compatible_base_url() == "https://ollama.example.com/v1"


def test_ollama_api_key_placeholder_when_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert config.ollama_api_key() == "ollama"
    monkeypatch.setenv("OLLAMA_API_KEY", "real-key")
    assert config.ollama_api_key() == "real-key"


# ---------------------------------------------------------------------------
# Client lifecycle (faked builder — no graphiti_core / Neo4j)
# ---------------------------------------------------------------------------
class _FakeGraphiti:
    def __init__(self):
        self.closed = False
        self.indices_built = 0

    async def build_indices_and_constraints(self):
        self.indices_built += 1

    async def close(self):
        self.closed = True


def test_get_graphiti_raises_when_disabled(monkeypatch):
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)
    with pytest.raises(GraphUnavailable):
        client_mod.get_graphiti()


def test_get_graphiti_is_cached_singleton(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    builds = []

    def _fake_build():
        g = _FakeGraphiti()
        builds.append(g)
        return g

    monkeypatch.setattr(client_mod, "_build_graphiti", _fake_build)
    first = client_mod.get_graphiti()
    second = client_mod.get_graphiti()
    assert first is second
    assert len(builds) == 1


def test_close_graphiti_resets_and_closes(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    fake = _FakeGraphiti()
    monkeypatch.setattr(client_mod, "_build_graphiti", lambda: fake)
    built = client_mod.get_graphiti()
    assert built is fake

    asyncio.run(client_mod.close_graphiti())
    assert fake.closed is True
    assert client_mod._graphiti is None


def test_close_graphiti_noop_when_never_built():
    # Safe to call with no client ever built.
    asyncio.run(client_mod.close_graphiti())


def _install_fake_graphiti_core(monkeypatch):
    """Inject minimal fake ``graphiti_core`` submodules into ``sys.modules``.

    Lets ``_build_graphiti`` run its real wiring (base_url/api_key/model
    plumbing + constructor) without the dependency installed. Each fake class
    records the kwargs it was built with so the test can assert the wiring.
    """
    captured: dict = {}

    class _Cap:
        def __init__(self, *args, **kwargs):
            captured.setdefault(type(self).__name__, []).append((args, kwargs))

    class Graphiti(_Cap):
        pass

    class OpenAIRerankerClient(_Cap):
        pass

    class OpenAIEmbedder(_Cap):
        pass

    class OpenAIEmbedderConfig(_Cap):
        pass

    class LLMConfig(_Cap):
        pass

    class OpenAIGenericClient(_Cap):
        pass

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    _mod("graphiti_core", Graphiti=Graphiti)
    _mod(
        "graphiti_core.cross_encoder",
    )
    _mod(
        "graphiti_core.cross_encoder.openai_reranker_client",
        OpenAIRerankerClient=OpenAIRerankerClient,
    )
    _mod(
        "graphiti_core.embedder",
    )
    _mod(
        "graphiti_core.embedder.openai",
        OpenAIEmbedder=OpenAIEmbedder,
        OpenAIEmbedderConfig=OpenAIEmbedderConfig,
    )
    _mod(
        "graphiti_core.llm_client",
    )
    _mod("graphiti_core.llm_client.config", LLMConfig=LLMConfig)
    _mod("graphiti_core.llm_client.openai_generic_client", OpenAIGenericClient=OpenAIGenericClient)
    return captured


def test_build_graphiti_wires_clients(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    monkeypatch.setenv("NEO4J_USER", "graph")
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    monkeypatch.setenv("LLM_BASE_URL", "https://ollama.example.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "key123")
    monkeypatch.setenv("GRAPHITI_LLM_MODEL", "extract-model")
    monkeypatch.setenv("GRAPHITI_EMBED_MODEL", "embed-model")
    monkeypatch.setenv("GRAPHITI_EMBED_DIM", "512")

    captured = _install_fake_graphiti_core(monkeypatch)
    graphiti = client_mod._build_graphiti()

    assert graphiti is not None
    # LLMConfig received the OpenAI-compatible endpoint, key, and model.
    _, llm_kwargs = captured["LLMConfig"][0]
    assert llm_kwargs["base_url"] == "https://ollama.example.com/v1"
    assert llm_kwargs["api_key"] == "key123"
    assert llm_kwargs["model"] == "extract-model"
    # Embedder config carried the embed model + dim.
    _, embed_kwargs = captured["OpenAIEmbedderConfig"][0]
    assert embed_kwargs["embedding_model"] == "embed-model"
    assert embed_kwargs["embedding_dim"] == 512
    # Graphiti got the bolt URL + creds positionally.
    g_args, _ = captured["Graphiti"][0]
    assert g_args[0] == "bolt://neo4j:7687"
    assert g_args[1] == "graph"
    assert g_args[2] == "pw"


def test_get_graphiti_uses_real_builder_path(monkeypatch):
    # Exercises get_graphiti caching through the real _build_graphiti with fakes.
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    _install_fake_graphiti_core(monkeypatch)
    first = client_mod.get_graphiti()
    second = client_mod.get_graphiti()
    assert first is second


# ---------------------------------------------------------------------------
# Schema registration
# ---------------------------------------------------------------------------
def test_graph_schema_identity():
    assert isinstance(GRAPH_SCHEMA, GraphSchema)
    assert GRAPH_SCHEMA.name == "agent_cognition_knowledge_graph"


def test_register_graph_indices_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)
    assert asyncio.run(register_graph_indices()) is False


def test_register_graph_indices_builds_when_enabled(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    fake = _FakeGraphiti()
    monkeypatch.setattr(client_mod, "_build_graphiti", lambda: fake)
    assert asyncio.run(register_graph_indices()) is True
    assert fake.indices_built == 1


# ---------------------------------------------------------------------------
# Timing decorator
# ---------------------------------------------------------------------------
def test_timed_graph_op_sync_passthrough():
    @timed_graph_op()
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_timed_graph_op_async_passthrough():
    @timed_graph_op("custom")
    async def mul(a, b):
        return a * b

    assert asyncio.run(mul(2, 3)) == 6


def test_timed_graph_op_sync_reraises():
    @timed_graph_op()
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        boom()


def test_timed_graph_op_async_reraises():
    @timed_graph_op()
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        asyncio.run(boom())


def test_timed_graph_op_logs_slow_call(monkeypatch):
    # Threshold of 0 forces the slow-call (info) branch for any duration.
    monkeypatch.setenv("NEO4J_SLOW_OP_MS", "0")

    @timed_graph_op()
    def quick():
        return "ok"

    assert quick() == "ok"


def test_slow_threshold_garbage_falls_back(monkeypatch):
    from shared_neo4j import metrics as metrics_mod

    monkeypatch.setenv("NEO4J_SLOW_OP_MS", "not-a-number")
    assert metrics_mod._slow_threshold_ms() == 1000.0


def test_graphiti_llm_model_fallback_on_resolver_error(monkeypatch):
    # Force the llm_service import/resolve to fail so the defensive fallback runs.
    import builtins

    monkeypatch.delenv("GRAPHITI_LLM_MODEL", raising=False)
    real_import = builtins.__import__

    def _boom_import(name, *args, **kwargs):
        if name == "llm_service.config":
            raise ImportError("simulated missing llm_service")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom_import)
    assert config.graphiti_llm_model() == "deepseek-v4-pro:cloud"


def test_llm_base_url_fallback_on_resolver_error(monkeypatch):
    import builtins

    monkeypatch.setenv("LLM_BASE_URL", "https://fallback.example.com/")
    real_import = builtins.__import__

    def _boom_import(name, *args, **kwargs):
        if name == "llm_service.config":
            raise ImportError("simulated missing llm_service")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom_import)
    assert config.llm_base_url() == "https://fallback.example.com"
