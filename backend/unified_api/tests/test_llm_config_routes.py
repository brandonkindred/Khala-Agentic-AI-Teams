"""Tests for /api/llm-config GET/PUT — masking, validation, persistence, caches.

Builds a minimal app with just the llm_config router so the whole unified_api
graph (and every team dependency) need not import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from unified_api.routes import llm_config as route  # noqa: E402


@pytest.fixture
def app_client(monkeypatch):
    """A TestClient over a minimal app, with the secret store + caches stubbed."""
    calls: dict = {"set": [], "cache_clears": 0, "runtime_clears": 0}
    monkeypatch.setattr(route, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(route, "set_secret", lambda svc, key, val: calls["set"].append((svc, key, val)))
    monkeypatch.setattr(
        route, "clear_client_cache", lambda: calls.__setitem__("cache_clears", calls["cache_clears"] + 1)
    )
    monkeypatch.setattr(
        route.runtime_config, "clear_cache", lambda: calls.__setitem__("runtime_clears", calls["runtime_clears"] + 1)
    )
    # Make resolvers deterministic regardless of host env.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    for var in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "LLM_CLAUDE_API_KEY",
        "OLLAMA_API_KEY",
        "LLM_OLLAMA_API_KEY",
        "LLM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app), calls, monkeypatch


def test_get_returns_defaults_and_options(app_client):
    client, _calls, _mp = app_client
    resp = client.get("/api/llm-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "ollama"
    assert "ollama" in body["provider_options"] and "claude" in body["provider_options"]
    assert "claude-opus-4-8" in body["claude_model_options"]
    assert body["claude_api_key_configured"] is False


def test_get_masks_keys_but_reports_configured(app_client):
    client, _calls, mp = app_client
    mp.setenv("LLM_PROVIDER", "claude")
    mp.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
    resp = client.get("/api/llm-config")
    body = resp.json()
    assert body["provider"] == "claude"
    assert body["claude_api_key_configured"] is True
    # The key must never appear anywhere in the response.
    assert "sk-super-secret" not in resp.text


def test_put_persists_and_clears_caches(app_client):
    client, calls, _mp = app_client
    resp = client.put(
        "/api/llm-config",
        json={"provider": "claude", "model": "claude-opus-4-8", "claude_api_key": "sk-new"},
    )
    assert resp.status_code == 200
    stored = dict((k, v) for _s, k, v in calls["set"])
    assert stored[route.runtime_config.KEY_PROVIDER] == "claude"
    assert stored[route.runtime_config.KEY_CLAUDE_MODEL] == "claude-opus-4-8"
    assert stored[route.runtime_config.KEY_CLAUDE_API_KEY] == "sk-new"
    assert calls["cache_clears"] == 1
    assert calls["runtime_clears"] == 1


def test_put_skips_empty_fields(app_client):
    client, calls, _mp = app_client
    client.put("/api/llm-config", json={"provider": "claude"})
    keys = [k for _s, k, _v in calls["set"]]
    # provider always written; empty model/keys are NOT written (preserve existing).
    assert route.runtime_config.KEY_PROVIDER in keys
    assert route.runtime_config.KEY_CLAUDE_API_KEY not in keys
    assert route.runtime_config.KEY_CLAUDE_MODEL not in keys
    assert route.runtime_config.KEY_OLLAMA_MODEL not in keys


def test_put_rejects_invalid_provider(app_client):
    client, _calls, _mp = app_client
    resp = client.put("/api/llm-config", json={"provider": "openai"})
    assert resp.status_code == 422  # Literal validation


def test_put_requires_postgres(app_client, monkeypatch):
    client, calls, _mp = app_client
    monkeypatch.setattr(route, "is_postgres_enabled", lambda: False)
    resp = client.put("/api/llm-config", json={"provider": "claude"})
    assert resp.status_code == 503
    assert calls["set"] == []  # nothing persisted


def test_get_reports_resolved_ollama_model(app_client):
    # _build_response routes through resolve_model_for_provider, so the effective
    # Ollama model (here from env) is surfaced — the UI never disagrees with agents.
    client, _calls, mp = app_client
    mp.setenv("LLM_PROVIDER", "ollama")
    mp.setenv("LLM_MODEL", "llama3.2")
    body = client.get("/api/llm-config").json()
    assert body["provider"] == "ollama"
    assert body["model"] == "llama3.2"


def test_put_stores_model_under_provider_specific_key(app_client):
    # The model is persisted under the active provider's key (ollama here), so it
    # never collides with a Claude selection in a shared slot.
    client, calls, _mp = app_client
    client.put("/api/llm-config", json={"provider": "ollama", "model": "llama3.2"})
    stored = {k: v for _s, k, v in calls["set"]}
    assert stored[route.runtime_config.KEY_OLLAMA_MODEL] == "llama3.2"
    assert route.runtime_config.KEY_CLAUDE_MODEL not in stored


def test_provider_model_keys_cover_all_provider_options():
    # Every provider the UI offers must have a storage key in PROVIDER_MODEL_KEYS,
    # else PUT would KeyError (500) on save. Asserting equality makes the map an
    # enforced single source: adding a provider to _PROVIDER_OPTIONS without a model
    # key (or vice versa) fails here instead of in production.
    assert set(route.runtime_config.PROVIDER_MODEL_KEYS) == set(route._PROVIDER_OPTIONS)
