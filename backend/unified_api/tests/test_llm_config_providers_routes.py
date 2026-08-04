"""Tests for /api/llm-config/providers — the ordered multi-provider fallback list:
masking, per-entry guards, CRUD, reorder, and the Postgres-required contract."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
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

from llm_service import provider_store as ps  # noqa: E402
from unified_api.routes import llm_config as route  # noqa: E402


def _entry(entry_id, *, provider="ollama", label="e", api_key="", limit=False):
    return ps.ProviderEntry(
        id=entry_id,
        label=label,
        provider=provider,
        model="m",
        base_url="http://localhost:11434" if provider == "ollama" else "",
        api_key=api_key,
        sort_order=entry_id,
        limit_exceeded=limit,
        limit_type="rate" if limit else "",
        reset_at=datetime(2026, 6, 30, tzinfo=timezone.utc) if limit else None,
    )


@pytest.fixture
def app_client(monkeypatch):
    """TestClient over a minimal app with provider_store + caches stubbed."""
    state: dict = {"entries": [], "ops": []}

    monkeypatch.setattr(route, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(route, "resolve_storage_status", lambda *a, **k: "available")
    monkeypatch.setattr(route, "clear_client_cache", lambda: state["ops"].append("clear_clients"))
    monkeypatch.setattr(route.runtime_config, "clear_cache", lambda: None)

    # provider_store stubs (no DB).
    monkeypatch.setattr(route.provider_store, "clear_cache", lambda: None)
    monkeypatch.setattr(route.provider_store, "load_ordered_entries", lambda *a, **k: list(state["entries"]))

    def fake_create(**kw):
        state["ops"].append(("create", kw))
        e = _entry(99, provider=kw["provider"], label=kw["label"], api_key=kw.get("api_key", ""))
        state["entries"].append(e)
        return e

    def fake_get(entry_id):
        return next((e for e in state["entries"] if e.id == entry_id), None)

    def fake_update(entry_id, **kw):
        state["ops"].append(("update", entry_id, kw))
        return fake_get(entry_id)

    def fake_delete(entry_id):
        e = fake_get(entry_id)
        if e is None:
            return False
        state["entries"].remove(e)
        return True

    def fake_reorder(ids):
        # Mirror the real store: validate the permutation atomically and raise on
        # mismatch (the route maps ReorderMismatchError -> 400).
        live = {e.id for e in state["entries"]}
        if len(ids) != len(live) or set(ids) != live:
            raise ps.ReorderMismatchError("not a permutation")
        state["ops"].append(("reorder", list(ids)))

    monkeypatch.setattr(route.provider_store, "create_entry", lambda **kw: fake_create(**kw))
    monkeypatch.setattr(route.provider_store, "get_entry", fake_get)
    monkeypatch.setattr(route.provider_store, "update_entry", lambda i, **kw: fake_update(i, **kw))
    monkeypatch.setattr(route.provider_store, "delete_entry", fake_delete)
    monkeypatch.setattr(route.provider_store, "reorder", fake_reorder)

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
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app), state


def test_list_masks_keys_and_reports_state(app_client):
    client, state = app_client
    state["entries"] = [_entry(1, api_key="sk-secret", limit=True), _entry(2)]
    resp = client.get("/api/llm-config/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body["providers"]] == [1, 2]
    assert body["providers"][0]["api_key_configured"] is True
    assert body["providers"][0]["limit_exceeded"] is True
    assert body["providers"][0]["reset_at"] is not None
    assert body["providers"][1]["api_key_configured"] is False
    assert "sk-secret" not in resp.text  # key value never returned
    assert body["storage_available"] is True


def test_list_degrades_gracefully_on_read_error(app_client, monkeypatch):
    client, _state = app_client

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(route.provider_store, "load_ordered_entries", boom)
    resp = client.get("/api/llm-config/providers")
    # The endpoint honors its "never raises" contract: 200 with an empty list and an
    # unreachable status instead of a 500.
    assert resp.status_code == 200
    body = resp.json()
    assert body["providers"] == []
    assert body["storage_available"] is False
    assert body["storage_status"] == "unreachable"


def test_create_ollama_local_succeeds(app_client):
    client, state = app_client
    resp = client.post(
        "/api/llm-config/providers",
        json={"label": "Local", "provider": "ollama", "base_url": "http://localhost:11434"},
    )
    assert resp.status_code == 200
    assert any(op[0] == "create" for op in state["ops"] if isinstance(op, tuple))
    assert ("clear_clients") in state["ops"]


def test_create_claude_without_key_is_400(app_client):
    client, _state = app_client
    resp = client.post("/api/llm-config/providers", json={"label": "C", "provider": "claude"})
    assert resp.status_code == 400
    assert "without an API key" in resp.json()["detail"]


def test_create_claude_with_key_succeeds(app_client):
    client, _state = app_client
    resp = client.post(
        "/api/llm-config/providers",
        json={"label": "C", "provider": "claude", "api_key": "sk-ant"},
    )
    assert resp.status_code == 200


def test_create_ollama_cloud_without_key_is_400(app_client):
    client, _state = app_client
    resp = client.post(
        "/api/llm-config/providers",
        json={"label": "Cloud", "provider": "ollama", "base_url": "https://ollama.com"},
    )
    assert resp.status_code == 400
    assert "Ollama Cloud" in resp.json()["detail"]


def test_create_rejects_malformed_base_url(app_client):
    client, _state = app_client
    resp = client.post(
        "/api/llm-config/providers",
        json={"label": "Bad", "provider": "ollama", "base_url": "not-a-url"},
    )
    assert resp.status_code == 422  # pydantic validation


def test_update_existing_entry(app_client):
    client, state = app_client
    state["entries"] = [_entry(1)]
    resp = client.put("/api/llm-config/providers/1", json={"label": "Renamed"})
    assert resp.status_code == 200
    assert any(op[0] == "update" for op in state["ops"] if isinstance(op, tuple))


def test_update_missing_entry_is_404(app_client):
    client, _state = app_client
    resp = client.put("/api/llm-config/providers/777", json={"label": "x"})
    assert resp.status_code == 404


def test_update_empty_text_fields_are_left_unchanged(app_client):
    client, state = app_client
    state["entries"] = [_entry(1, provider="ollama")]
    # Empty strings mean "leave unchanged" per the contract — normalized to None so the
    # store never clears the stored value.
    resp = client.put("/api/llm-config/providers/1", json={"label": "  ", "model": "", "base_url": ""})
    assert resp.status_code == 200
    update_op = next(op for op in state["ops"] if isinstance(op, tuple) and op[0] == "update")
    kw = update_op[2]
    assert kw["label"] is None and kw["model"] is None and kw["base_url"] is None


def test_update_empty_label_is_not_422(app_client):
    client, state = app_client
    state["entries"] = [_entry(1)]
    # An empty label must be treated as "no change", not rejected with a 422.
    resp = client.put("/api/llm-config/providers/1", json={"label": ""})
    assert resp.status_code == 200


def test_update_sets_non_empty_fields(app_client):
    client, state = app_client
    state["entries"] = [_entry(1, provider="ollama")]
    resp = client.put(
        "/api/llm-config/providers/1", json={"label": "New", "model": "qwen", "base_url": "http://h:11434"}
    )
    assert resp.status_code == 200
    kw = next(op for op in state["ops"] if isinstance(op, tuple) and op[0] == "update")[2]
    assert kw["label"] == "New" and kw["model"] == "qwen" and kw["base_url"] == "http://h:11434"


def test_update_clear_api_key_removes_stored_key(app_client):
    client, state = app_client
    state["entries"] = [_entry(1, provider="ollama", api_key="oldkey")]  # local Ollama needs no key
    resp = client.put("/api/llm-config/providers/1", json={"clear_api_key": True})
    assert resp.status_code == 200
    kw = next(op for op in state["ops"] if isinstance(op, tuple) and op[0] == "update")[2]
    assert kw["api_key"] == ""  # explicit removal (vs None = unchanged)


def test_update_clear_api_key_ignored_when_new_key_given(app_client):
    client, state = app_client
    state["entries"] = [_entry(1, provider="ollama", api_key="old")]
    resp = client.put("/api/llm-config/providers/1", json={"clear_api_key": True, "api_key": "newkey"})
    assert resp.status_code == 200
    kw = next(op for op in state["ops"] if isinstance(op, tuple) and op[0] == "update")[2]
    assert kw["api_key"] == "newkey"  # a provided key wins over the clear flag


def test_update_clear_api_key_on_claude_without_fallback_is_400(app_client):
    client, state = app_client
    state["entries"] = [_entry(1, provider="claude", api_key="old")]
    # Clearing a Claude entry's only key (no env fallback) leaves it unusable → 400.
    resp = client.put("/api/llm-config/providers/1", json={"clear_api_key": True})
    assert resp.status_code == 400


def test_update_to_claude_without_key_is_400(app_client):
    client, state = app_client
    state["entries"] = [_entry(1, provider="ollama")]
    resp = client.put("/api/llm-config/providers/1", json={"provider": "claude"})
    assert resp.status_code == 400


def test_delete_entry(app_client):
    client, state = app_client
    state["entries"] = [_entry(1)]
    resp = client.delete("/api/llm-config/providers/1")
    assert resp.status_code == 200
    assert resp.json()["providers"] == []


def test_delete_missing_is_404(app_client):
    client, _state = app_client
    resp = client.delete("/api/llm-config/providers/42")
    assert resp.status_code == 404


def test_reorder_calls_store(app_client):
    client, state = app_client
    state["entries"] = [_entry(1), _entry(2), _entry(3)]
    resp = client.put("/api/llm-config/providers/order", json={"ids": [3, 1, 2]})
    assert resp.status_code == 200
    assert ("reorder", [3, 1, 2]) in state["ops"]


def test_reorder_rejects_non_permutation(app_client):
    client, state = app_client
    state["entries"] = [_entry(1), _entry(2), _entry(3)]
    # Missing an id, an unknown id, and a duplicate (wrong length) are all rejected.
    assert client.put("/api/llm-config/providers/order", json={"ids": [1, 2]}).status_code == 400
    assert client.put("/api/llm-config/providers/order", json={"ids": [1, 2, 99]}).status_code == 400
    assert client.put("/api/llm-config/providers/order", json={"ids": [1, 2, 2]}).status_code == 400
    # No store mutation happened for any rejected request.
    assert not any(op[0] == "reorder" for op in state["ops"] if isinstance(op, tuple))
    # A correct permutation succeeds.
    assert client.put("/api/llm-config/providers/order", json={"ids": [3, 2, 1]}).status_code == 200


def test_mutations_require_postgres(app_client, monkeypatch):
    client, _state = app_client
    monkeypatch.setattr(route, "is_postgres_enabled", lambda: False)
    assert client.post("/api/llm-config/providers", json={"label": "x", "provider": "ollama"}).status_code == 503
    assert client.put("/api/llm-config/providers/1", json={"label": "x"}).status_code == 503
    assert client.delete("/api/llm-config/providers/1").status_code == 503
    assert client.put("/api/llm-config/providers/order", json={"ids": [1]}).status_code == 503


# --------------------------------------------------------------------------- #
# Entry credentials guard: an entry must carry its OWN key — NO env fallback   #
# (the provider list is the sole source; a keyless Claude/Cloud entry is       #
# unusable at call time, so persisting one is rejected).                        #
# --------------------------------------------------------------------------- #


def test_create_claude_without_key_is_400_even_with_env_key(app_client, monkeypatch):
    """A Claude entry with an empty api_key is rejected even when ANTHROPIC_API_KEY is
    set in env — entries are self-contained, there is no env fallback at call time."""
    client, _state = app_client
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    monkeypatch.setenv("LLM_CLAUDE_API_KEY", "sk-env")
    resp = client.post("/api/llm-config/providers", json={"label": "C", "provider": "claude"})
    assert resp.status_code == 400
    assert "without an API key" in resp.json()["detail"]


def test_create_ollama_cloud_without_key_is_400_even_with_env_key(app_client, monkeypatch):
    """An Ollama-Cloud entry with an empty api_key is rejected even when OLLAMA_API_KEY
    is set in env — no env fallback for the entry's own key."""
    client, _state = app_client
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-env")
    monkeypatch.setenv("LLM_OLLAMA_API_KEY", "sk-env")
    resp = client.post(
        "/api/llm-config/providers",
        json={"label": "Cloud", "provider": "ollama", "base_url": "https://ollama.com"},
    )
    assert resp.status_code == 400
    assert "Ollama Cloud" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# /ollama-models — the browse utility endpoint (kept; single-provider config    #
# GET/PUT was removed).                                                         #
# --------------------------------------------------------------------------- #


def test_ollama_models_live_listing(app_client, monkeypatch):
    """A non-empty live listing from /api/tags is returned verbatim with source=live."""
    client, _state = app_client
    monkeypatch.setattr(route, "list_ollama_models", lambda: ["llama3.2", "qwen3-coder:480b-cloud"])
    resp = client.get("/api/llm-config/ollama-models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["models"] == ["llama3.2", "qwen3-coder:480b-cloud"]
    # base_url reflects the resolved effective endpoint (cloud default with no env).
    assert body["base_url"] == "https://ollama.com"


def test_ollama_models_falls_back_to_curated(app_client, monkeypatch):
    """An empty live listing degrades to the curated suggestions (source=fallback)."""
    client, _state = app_client
    monkeypatch.setattr(route, "list_ollama_models", lambda: [])
    resp = client.get("/api/llm-config/ollama-models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    assert body["models"] == list(route._OLLAMA_MODEL_SUGGESTIONS)
    assert body["models"]  # non-empty curated fallback


def test_ollama_models_base_url_reflects_resolved_endpoint(app_client, monkeypatch):
    """The reported base_url tracks the resolved Ollama endpoint (env override here)."""
    client, _state = app_client
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434")
    monkeypatch.setattr(route, "list_ollama_models", lambda: ["glm-5.2:cloud"])
    body = client.get("/api/llm-config/ollama-models").json()
    assert body["base_url"] == "http://localhost:11434"
