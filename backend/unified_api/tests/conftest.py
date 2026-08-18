"""Shared fixtures for unified_api.routes.llm_config tests: the app_client
fixture stubs provider_store, Postgres, and cache-clear entry points so
/api/llm-config/providers CRUD tests never touch a real database."""

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


def _entry(entry_id, *, provider="ollama", label="e", api_key="", limit=False, base_url=None):
    return ps.ProviderEntry(
        id=entry_id,
        label=label,
        provider=provider,
        model="m",
        base_url=base_url if base_url is not None else ("http://localhost:11434" if provider == "ollama" else ""),
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
