"""Hermetic route-level tests for /api/user-profile.

Storage functions (get_profile, upsert_profile, list_associations,
get_integrations_list) are monkeypatched directly on the routes module so
these tests never touch Postgres.
"""

from __future__ import annotations

import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unified_api.routes.user_profile as routes_mod
from user_profile import Association, UserProfile


def _profile() -> UserProfile:
    return UserProfile(user_id="default", display_name="Ada", email="ada@example.com")


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes_mod.router)
    return TestClient(app)


def test_read_profile(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_mod, "get_profile", lambda user_id: _profile())
    resp = client.get("/api/user-profile")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Ada"


def test_read_profile_storage_unavailable_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(user_id):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(routes_mod, "get_profile", _boom)
    resp = client.get("/api/user-profile")
    assert resp.status_code == 503


def test_update_profile(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_mod, "upsert_profile", lambda update, user_id: _profile())
    resp = client.put("/api/user-profile", json={"display_name": "Ada"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Ada"


def test_update_profile_storage_unavailable_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(update, user_id):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(routes_mod, "upsert_profile", _boom)
    resp = client.put("/api/user-profile", json={})
    assert resp.status_code == 503


def test_read_associations(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assoc = Association(id="a1", user_id="default", artifact_type="brand", team="branding", artifact_id="b1")
    monkeypatch.setattr(routes_mod, "list_associations", lambda user_id, artifact_type: [assoc])
    resp = client.get("/api/user-profile/associations")
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "a1"


def test_read_associations_filtered_by_type(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _list(user_id, artifact_type):
        captured["artifact_type"] = artifact_type
        return []

    monkeypatch.setattr(routes_mod, "list_associations", _list)
    resp = client.get("/api/user-profile/associations", params={"artifact_type": "brand"})
    assert resp.status_code == 200
    assert captured["artifact_type"].value == "brand"


def test_read_associations_storage_unavailable_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(user_id, artifact_type):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(routes_mod, "list_associations", _boom)
    resp = client.get("/api/user-profile/associations")
    assert resp.status_code == 503


def test_read_integrations_empty_on_error(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise RuntimeError("integrations store unavailable")

    monkeypatch.setattr(routes_mod, "get_integrations_list", _boom)
    resp = client.get("/api/user-profile/integrations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_read_integrations_skips_invalid_items(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        routes_mod,
        "get_integrations_list",
        lambda: [
            {"id": "slack", "type": "slack", "enabled": True, "channel": "#general"},
            {"id": "broken"},  # missing required 'type'/'enabled' -> skipped
        ],
    )
    resp = client.get("/api/user-profile/integrations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "slack"


def test_read_overview(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_mod, "get_profile", lambda user_id: _profile())
    monkeypatch.setattr(routes_mod, "list_associations", lambda user_id: [])
    monkeypatch.setattr(routes_mod, "get_integrations_list", lambda: [])
    resp = client.get("/api/user-profile/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["display_name"] == "Ada"
    assert body["associations"] == []
    assert body["integrations"] == []


def test_read_overview_storage_unavailable_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(user_id):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(routes_mod, "get_profile", _boom)
    resp = client.get("/api/user-profile/overview")
    assert resp.status_code == 503
