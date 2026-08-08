"""Tests that update/delete form-record endpoints enforce the form_key path segment.

Previously update_team_form_record and delete_team_form_record accepted
form_key from the URL but never passed it to infra.form_store, mutating
records by record_id alone. A request to
/teams/{team_id}/forms/{form_key}/{record_id} could therefore affect a record
belonging to a different form under the same team.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture(autouse=True)
def _isolate_agent_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    import agent_team_studio.agentic_team_provisioning.infrastructure as infra_mod

    infra_mod._set_agent_cache_for_testing(str(tmp_path))
    infra_mod._clear_infra_cache_for_testing()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _seed_team() -> str:
    store = AgenticTeamStore()
    team = store.create_team(name="Ops", description="")
    return team.team_id


def test_update_record_via_wrong_form_key_returns_404_and_does_not_mutate(client: TestClient):
    team_id = _seed_team()

    create_resp = client.post(f"/teams/{team_id}/forms/intake", json={"data": {"name": "Alice"}})
    assert create_resp.status_code == 201
    record_id = create_resp.json()["record_id"]

    # A second, unrelated form under the same team.
    client.post(f"/teams/{team_id}/forms/survey", json={"data": {"q": "unrelated"}})

    resp = client.put(
        f"/teams/{team_id}/forms/survey/{record_id}",
        json={"data": {"name": "Mallory"}},
    )
    assert resp.status_code == 404

    # The record is unchanged and still lives under its real form_key.
    records = client.get(f"/teams/{team_id}/forms/intake").json()
    assert len(records) == 1
    assert records[0]["record_id"] == record_id
    assert records[0]["data"]["name"] == "Alice"


def test_delete_record_via_wrong_form_key_returns_404_and_does_not_mutate(client: TestClient):
    team_id = _seed_team()

    create_resp = client.post(f"/teams/{team_id}/forms/intake", json={"data": {"name": "Alice"}})
    record_id = create_resp.json()["record_id"]
    client.post(f"/teams/{team_id}/forms/survey", json={"data": {"q": "unrelated"}})

    resp = client.delete(f"/teams/{team_id}/forms/survey/{record_id}")
    assert resp.status_code == 404

    records = client.get(f"/teams/{team_id}/forms/intake").json()
    assert len(records) == 1
    assert records[0]["record_id"] == record_id


def test_update_and_delete_via_correct_form_key_succeed(client: TestClient):
    team_id = _seed_team()

    create_resp = client.post(f"/teams/{team_id}/forms/intake", json={"data": {"name": "Alice"}})
    record_id = create_resp.json()["record_id"]

    update_resp = client.put(
        f"/teams/{team_id}/forms/intake/{record_id}",
        json={"data": {"name": "Bob"}},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["data"]["name"] == "Bob"

    delete_resp = client.delete(f"/teams/{team_id}/forms/intake/{record_id}")
    assert delete_resp.status_code == 204
    assert client.get(f"/teams/{team_id}/forms/intake").json() == []
