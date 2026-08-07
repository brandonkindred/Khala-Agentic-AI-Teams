"""Tests for GET /teams/{team_id}/agents/manifests (stamped cognition manifests)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Fake Postgres must be installed before the API handlers touch the store.
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return TestClient(app)


def _seed_team_with_agents() -> str:
    store = AgenticTeamStore()
    team = store.create_team(name="Support", description="")
    store.save_team_agents(
        team.team_id,
        [
            AgenticTeamAgent(agent_name="Triage Agent", role="Classifies tickets"),
            AgenticTeamAgent(agent_name="Router Agent", role="Routes tickets"),
        ],
    )
    return team.team_id


def test_list_manifests_returns_stamped_cognition(client: TestClient):
    team_id = _seed_team_with_agents()

    resp = client.get(f"/teams/{team_id}/agents/manifests")
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_id"] == team_id
    assert len(body["manifests"]) == 2
    for manifest in body["manifests"]:
        assert manifest["team"] == "agentic_team_provisioning"
        assert manifest["cognition"]["rule_packs"] == ["default_guardrails"]
        assert manifest["cognition"]["memory"]["retention_days_events"] == 90
        assert manifest["cognition"]["tools"] == []


def test_list_manifests_empty_roster(client: TestClient):
    store = AgenticTeamStore()
    team = store.create_team(name="Empty", description="")
    resp = client.get(f"/teams/{team.team_id}/agents/manifests")
    assert resp.status_code == 200
    assert resp.json()["manifests"] == []


def test_list_manifests_unknown_team_404(client: TestClient):
    resp = client.get("/teams/does-not-exist/agents/manifests")
    assert resp.status_code == 404
