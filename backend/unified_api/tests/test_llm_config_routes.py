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
    assert stored[route.runtime_config.KEY_MODEL] == "claude-opus-4-8"
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
    assert route.runtime_config.KEY_MODEL not in keys


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
