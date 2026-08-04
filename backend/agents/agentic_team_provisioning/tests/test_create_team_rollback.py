"""``POST /teams`` rolls back the team row if infrastructure provisioning fails.

Covers the compensating-transaction fix for the pre-existing bug where
``create_team`` committed the team row via ``_store.create_team`` before
calling ``provision_team`` — leaving an orphaned, infrastructure-less row
behind whenever provisioning raised.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_team_provisioning.assistant.store import AgenticTeamStore
from agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    db = install_fake_postgres(monkeypatch)
    # Best-effort profile association is orthogonal to this test and lives in a
    # separate (unfaked) Postgres store; no-op it like test_profile_association.py.
    import agentic_team_provisioning.assistant.store as store_mod

    monkeypatch.setattr(store_mod, "record_association_safe", lambda *a, **k: None)
    monkeypatch.setattr(store_mod, "remove_association_safe", lambda *a, **k: None)
    return db


@pytest.fixture
def client(fake_pg: dict) -> TestClient:
    from agentic_team_provisioning.api.main import app

    return TestClient(app)


def test_create_team_rolls_back_on_provisioning_failure(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import agentic_team_provisioning.api.main as main_mod

    def _boom(team_id: str):
        raise RuntimeError("disk full")

    monkeypatch.setattr(main_mod, "provision_team", _boom)

    resp = client.post("/teams", json={"name": "Growth Pod", "description": ""})

    assert resp.status_code == 500
    # No orphaned row: the failed create must not leave a listable team behind.
    assert fake_pg["teams"] == {}
    assert AgenticTeamStore().list_teams() == []


def test_create_team_persists_on_provisioning_success(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import agentic_team_provisioning.api.main as main_mod

    monkeypatch.setattr(main_mod, "provision_team", lambda team_id: None)

    resp = client.post("/teams", json={"name": "Growth Pod", "description": ""})

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Growth Pod"
    assert AgenticTeamStore().get_team(body["team_id"]) is not None
