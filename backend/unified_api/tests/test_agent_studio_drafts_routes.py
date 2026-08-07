"""Hermetic TestClient tests for Agent Studio drafts HTTP routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore
from unified_api.routes import agent_studio as routes


@pytest.fixture()
def store() -> AgentStudioDraftStore:
    return AgentStudioDraftStore()


@pytest.fixture()
def client(store: AgentStudioDraftStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(routes, "get_draft_store", lambda: store)
    app = FastAPI()
    app.include_router(routes.router)
    # Default user is whatever get_current_user_id returns; override per-test when needed.
    return TestClient(app)


def _as_user(client: TestClient, user_id: str) -> None:
    client.app.dependency_overrides[routes.get_current_user_id] = lambda: user_id


def test_create_list_get_round_trip(client: TestClient) -> None:
    _as_user(client, "alice")
    created = client.post("/api/agent-studio/drafts", json={"name": "Alpha", "payload": {"teamId": "t1"}})
    assert created.status_code == 200
    summary = created.json()
    assert summary["name"] == "Alpha"
    assert "draft_id" in summary and "updated_at" in summary
    assert "payload" not in summary

    listed = client.get("/api/agent-studio/drafts")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["draft_id"] == summary["draft_id"]

    full = client.get(f"/api/agent-studio/drafts/{summary['draft_id']}")
    assert full.status_code == 200
    body = full.json()
    assert body["payload"] == {"teamId": "t1"}
    assert body["created_at"]


def test_put_updates_owned_draft(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post("/api/agent-studio/drafts", json={"name": "Old"}).json()["draft_id"]
    updated = client.put(
        f"/api/agent-studio/drafts/{draft_id}",
        json={"name": "New", "payload": {"a": 2}},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "New"
    full = client.get(f"/api/agent-studio/drafts/{draft_id}").json()
    assert full["payload"] == {"a": 2}


def test_put_missing_returns_404(client: TestClient) -> None:
    _as_user(client, "alice")
    resp = client.put("/api/agent-studio/drafts/missing", json={"name": "x"})
    assert resp.status_code == 404


def test_rename_and_delete(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post("/api/agent-studio/drafts", json={"name": "Old"}).json()["draft_id"]
    renamed = client.patch(f"/api/agent-studio/drafts/{draft_id}", json={"name": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed"
    deleted = client.delete(f"/api/agent-studio/drafts/{draft_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"draft_id": draft_id, "status": "deleted"}
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").status_code == 404


def test_rename_empty_name_returns_422_or_400(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post("/api/agent-studio/drafts", json={"name": "Old"}).json()["draft_id"]
    resp = client.patch(f"/api/agent-studio/drafts/{draft_id}", json={"name": ""})
    assert resp.status_code in (400, 422)


def test_tenancy_isolation(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post("/api/agent-studio/drafts", json={"name": "Secret", "payload": {"x": 1}}).json()["draft_id"]

    _as_user(client, "bob")
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").status_code == 404
    assert client.put(f"/api/agent-studio/drafts/{draft_id}", json={"name": "Hijack"}).status_code == 404
    assert client.patch(f"/api/agent-studio/drafts/{draft_id}", json={"name": "Hijack"}).status_code == 404
    assert client.delete(f"/api/agent-studio/drafts/{draft_id}").status_code == 404
    assert client.get("/api/agent-studio/drafts").json() == []

    _as_user(client, "alice")
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").status_code == 200
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").json()["name"] == "Secret"


def test_list_pagination_query_params(client: TestClient) -> None:
    _as_user(client, "alice")
    for i in range(3):
        client.post("/api/agent-studio/drafts", json={"name": f"d{i}"})
    page = client.get("/api/agent-studio/drafts", params={"limit": 1, "offset": 1})
    assert page.status_code == 200
    assert len(page.json()) == 1
