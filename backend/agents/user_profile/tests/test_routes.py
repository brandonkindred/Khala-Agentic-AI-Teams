"""API tests for the user-profile router via FastAPI TestClient.

The router is mounted on a throwaway app so these tests don't depend on the
full unified_api startup. Persistence uses the dict-backed fake Postgres.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unified_api.routes.user_profile import router

from user_profile.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch):
    install_fake_postgres(monkeypatch)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_profile_returns_default(client):
    resp = client.get("/api/user-profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "default"
    assert body["display_name"] == ""


def test_put_profile_updates_fields(client):
    resp = client.put("/api/user-profile", json={"display_name": "Brandon", "bio": "builder"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Brandon"
    assert body["bio"] == "builder"
    # Persisted across requests.
    assert client.get("/api/user-profile").json()["display_name"] == "Brandon"


def test_associations_endpoint_lists_and_filters(client, monkeypatch):
    from user_profile import store as up_store

    up_store.record_association("brand", "branding", "brand_1", label="Acme")
    up_store.record_association("project", "coding_team", "job_2", label="Repo")

    resp = client.get("/api/user-profile/associations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "default"
    assert len(body["associations"]) == 2

    filtered = client.get("/api/user-profile/associations", params={"artifact_type": "brand"})
    items = filtered.json()["associations"]
    assert len(items) == 1
    assert items[0]["team"] == "branding"


def test_overview_aggregates_profile_associations_integrations(client, monkeypatch):
    import unified_api.integrations_store as istore

    from user_profile import store as up_store

    up_store.record_association("brand", "branding", "brand_1", label="Acme")
    monkeypatch.setattr(
        istore,
        "get_integrations_list",
        lambda: [{"id": "slack", "type": "slack", "enabled": True, "channel": "#eng"}],
    )

    resp = client.get("/api/user-profile/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["user_id"] == "default"
    assert len(body["associations"]) == 1
    assert body["associations"][0]["team"] == "branding"
    assert body["integrations"][0]["id"] == "slack"


def test_overview_storage_unavailable_returns_503(monkeypatch):
    import unified_api.routes.user_profile as routes_mod

    app = FastAPI()
    app.include_router(router)
    tc = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(routes_mod, "get_profile", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert tc.get("/api/user-profile/overview").status_code == 503


def test_integrations_passthrough(client, monkeypatch):
    import unified_api.integrations_store as istore

    monkeypatch.setattr(
        istore,
        "get_integrations_list",
        lambda: [{"id": "slack", "type": "slack", "enabled": True, "channel": "#eng"}],
    )
    resp = client.get("/api/user-profile/integrations")
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "slack"


def test_integrations_passthrough_tolerates_failure(client, monkeypatch):
    import unified_api.integrations_store as istore

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(istore, "get_integrations_list", _boom)
    resp = client.get("/api/user-profile/integrations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_profile_storage_unavailable_returns_503(monkeypatch):
    """When the store raises, the GET surfaces a clean 503."""
    import unified_api.routes.user_profile as routes_mod

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    def _boom(*a, **k):
        raise RuntimeError("postgres down")

    # The route imported get_profile into its own namespace; patch it there.
    monkeypatch.setattr(routes_mod, "get_profile", _boom)
    resp = client.get("/api/user-profile")
    assert resp.status_code == 503


@pytest.mark.parametrize(
    ("symbol", "method", "path", "kwargs"),
    [
        ("upsert_profile", "put", "/api/user-profile", {"json": {"display_name": "x"}}),
        ("list_associations", "get", "/api/user-profile/associations", {}),
    ],
)
def test_storage_unavailable_paths_return_503(monkeypatch, symbol, method, path, kwargs):
    import unified_api.routes.user_profile as routes_mod

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    def _boom(*a, **k):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(routes_mod, symbol, _boom)
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 503
