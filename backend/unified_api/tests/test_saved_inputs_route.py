"""Hermetic route-level tests for the saved-inputs endpoints.

A fake store with in-memory dict storage replaces the real one so tests
don't require a live Postgres.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_console.models import SavedInput
from agent_console.store import SavedInputNameConflict


class _FakeStore:
    def __init__(self) -> None:
        self._items: dict[str, SavedInput] = {}

    def list_saved_inputs(self, agent_id: str) -> list[SavedInput]:
        return sorted(
            (s for s in self._items.values() if s.agent_id == agent_id),
            key=lambda s: s.created_at,
            reverse=True,
        )

    def get_saved_input(self, saved_id: str) -> SavedInput | None:
        return self._items.get(saved_id)

    def create_saved_input(self, *, agent_id, name, input_data, author, description):
        if any(s.agent_id == agent_id and s.name == name for s in self._items.values()):
            raise SavedInputNameConflict(f"{agent_id} / {name}")
        now = datetime.now(tz=timezone.utc)
        saved = SavedInput(
            id=str(uuid4()),
            agent_id=agent_id,
            name=name,
            input_data=input_data,
            author=author,
            description=description,
            created_at=now,
            updated_at=now,
        )
        self._items[saved.id] = saved
        return saved

    def update_saved_input(self, saved_id, *, name=None, input_data=None, description=None):
        existing = self._items.get(saved_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                "name": name if name is not None else existing.name,
                "input_data": input_data if input_data is not None else existing.input_data,
                "description": description if description is not None else existing.description,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        self._items[saved_id] = updated
        return updated

    def delete_saved_input(self, saved_id):
        return self._items.pop(saved_id, None) is not None


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # Seed the registry with a known agent id.
    manifest_dir = tmp_path / "blogging" / "agent_console" / "manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "planner.yaml").write_text(
        "schema_version: 1\n"
        "id: blogging.planner\n"
        "team: blogging\n"
        "name: Planner\n"
        "summary: test\n"
        "source:\n"
        "  entrypoint: x:y\n",
        encoding="utf-8",
    )
    import agent_registry.loader as loader

    # Prior tests in the suite may have monkeypatched ``get_registry`` to a
    # plain lambda, which lacks ``cache_clear``. Restore the cached function
    # before taking our own snapshot so the side effect never outlives us.
    if not hasattr(loader.get_registry, "cache_clear"):
        import importlib

        importlib.reload(loader)
    loader.get_registry.cache_clear()
    rebuilt = loader.AgentRegistry.load(tmp_path)
    original_loader_get_registry = loader.get_registry
    loader.get_registry = lambda: rebuilt  # type: ignore[assignment]

    # Stub store + author on the route module.
    fake_store = _FakeStore()
    import unified_api.routes.agent_console_saved_inputs as routes_mod

    original_route_get_store = routes_mod.get_store
    original_route_resolve_author = routes_mod.resolve_author
    original_route_get_registry = routes_mod.get_registry
    routes_mod.get_store = lambda: fake_store  # type: ignore[assignment]
    routes_mod.resolve_author = lambda: "tester"  # type: ignore[assignment]
    routes_mod.get_registry = lambda: rebuilt  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(routes_mod.router)
    try:
        yield TestClient(app)
    finally:
        loader.get_registry = original_loader_get_registry  # type: ignore[assignment]
        routes_mod.get_store = original_route_get_store  # type: ignore[assignment]
        routes_mod.resolve_author = original_route_resolve_author  # type: ignore[assignment]
        routes_mod.get_registry = original_route_get_registry  # type: ignore[assignment]
        if hasattr(loader.get_registry, "cache_clear"):
            loader.get_registry.cache_clear()


def test_create_and_list_saved_input(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/blogging.planner/saved-inputs",
        json={"name": "seed", "input_data": {"brief": "hi"}, "description": "smoke"},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["name"] == "seed"
    assert created["author"] == "tester"

    resp = client.get("/api/agents/blogging.planner/saved-inputs")
    assert resp.status_code == 200
    assert [r["name"] for r in resp.json()] == ["seed"]


def test_create_with_unknown_agent_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/does.not.exist/saved-inputs",
        json={"name": "seed", "input_data": {}},
    )
    assert resp.status_code == 404


def test_duplicate_name_is_409(client: TestClient) -> None:
    client.post(
        "/api/agents/blogging.planner/saved-inputs",
        json={"name": "dup", "input_data": {}},
    )
    resp = client.post(
        "/api/agents/blogging.planner/saved-inputs",
        json={"name": "dup", "input_data": {}},
    )
    assert resp.status_code == 409


def test_update_and_delete_saved_input(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/blogging.planner/saved-inputs",
        json={"name": "keep", "input_data": {"a": 1}},
    )
    saved_id = resp.json()["id"]
    resp = client.put(
        f"/api/agents/saved-inputs/{saved_id}",
        json={"input_data": {"a": 2}},
    )
    assert resp.status_code == 200
    assert resp.json()["input_data"] == {"a": 2}

    resp = client.delete(f"/api/agents/saved-inputs/{saved_id}")
    assert resp.status_code == 200

    resp = client.get(f"/api/agents/saved-inputs/{saved_id}")
    assert resp.status_code == 404
