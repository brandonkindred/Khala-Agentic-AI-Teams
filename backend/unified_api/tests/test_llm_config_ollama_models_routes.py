"""Tests for /api/llm-config/ollama-models — the live/curated Ollama model browse
endpoint. Unlike the /api/llm-config/providers CRUD routes (see
test_llm_config_providers_routes.py and its shared conftest.py app_client
fixture), this endpoint has no Postgres or provider-store dependency — its
handler only calls llm_config.resolve_base_url() and list_ollama_models() — so
its fixture only needs to mount the router and isolate the base-url env var."""

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
    """TestClient over a minimal app with only the base-url env var isolated."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_ollama_models_live_listing(app_client, monkeypatch):
    """A non-empty live listing from /api/tags is returned verbatim with source=live."""
    monkeypatch.setattr(route, "list_ollama_models", lambda: ["deepseek-v4-flash:cloud", "qwen3-coder:480b-cloud"])
    resp = app_client.get("/api/llm-config/ollama-models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["models"] == ["deepseek-v4-flash:cloud", "qwen3-coder:480b-cloud"]
    # base_url reflects the resolved effective endpoint (cloud default with no env).
    assert body["base_url"] == "https://ollama.com"


def test_ollama_models_falls_back_to_curated(app_client, monkeypatch):
    """An empty live listing degrades to the curated suggestions (source=fallback)."""
    monkeypatch.setattr(route, "list_ollama_models", lambda: [])
    resp = app_client.get("/api/llm-config/ollama-models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    assert body["models"] == list(route._OLLAMA_MODEL_SUGGESTIONS)
    assert body["models"]  # non-empty curated fallback


def test_ollama_models_base_url_reflects_resolved_endpoint(app_client, monkeypatch):
    """The reported base_url tracks the resolved Ollama endpoint (env override here)."""
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434")
    monkeypatch.setattr(route, "list_ollama_models", lambda: ["deepseek-v4-flash:cloud"])
    body = app_client.get("/api/llm-config/ollama-models").json()
    assert body["base_url"] == "http://localhost:11434"
