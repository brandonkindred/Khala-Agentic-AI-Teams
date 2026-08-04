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
    """When provisioning fails, create_team must return 500 and remove the team row.

    Preconditions: the fake Postgres store is installed and ``provision_team``
        is patched to raise.
    Postconditions: the response status is 500, and no team row remains in
        the backing store (checked both via the fake db and ``list_teams``).
    """
    import agentic_team_provisioning.api.main as main_mod

    def _boom(team_id: str):
        raise RuntimeError("disk full")

    monkeypatch.setattr(main_mod, "provision_team", _boom)

    resp = client.post("/teams", json={"name": "Growth Pod", "description": ""})

    assert resp.status_code == 500
    # No orphaned row: the failed create must not leave a listable team behind.
    assert fake_pg["teams"] == {}
    assert AgenticTeamStore().list_teams() == []


def test_create_team_returns_500_when_rollback_delete_also_fails(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A rollback-delete failure must not mask the required 500 response.

    Preconditions: ``provision_team`` is patched to raise, and
        ``AgenticTeamStore.delete_team`` is separately patched to raise (e.g.
        simulating a DB connectivity problem during rollback).
    Postconditions: the response is still 500 — the compensating delete's own
        exception is caught and logged rather than propagating past the
        original provisioning error.
    """
    import agentic_team_provisioning.api.main as main_mod
    from agentic_team_provisioning.assistant.store import AgenticTeamStore as _Store

    monkeypatch.setattr(
        main_mod,
        "provision_team",
        lambda team_id: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    monkeypatch.setattr(
        _Store,
        "delete_team",
        lambda self, team_id: (_ for _ in ()).throw(RuntimeError("db unreachable")),
    )

    resp = client.post("/teams", json={"name": "Growth Pod", "description": ""})

    assert resp.status_code == 500


def test_create_team_persists_on_provisioning_success(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Happy-path regression: a successful provision must leave the team row intact.

    Preconditions: the fake Postgres store is installed and ``provision_team``
        is patched to succeed (no-op).
    Postconditions: the response status is 200 and the created team is
        retrievable from the store.
    """
    import agentic_team_provisioning.api.main as main_mod

    monkeypatch.setattr(main_mod, "provision_team", lambda team_id: None)

    resp = client.post("/teams", json={"name": "Growth Pod", "description": ""})

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Growth Pod"
    assert AgenticTeamStore().get_team(body["team_id"]) is not None
