"""Tests for GET /teams/{team_id}/agents/manifests (stamped cognition manifests)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    manifest_agent_id,
)
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Fake Postgres must be installed before the API handlers touch the store.
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return TestClient(app)


def _thin_generated(team_id: str, agent_name: str, *, summary: str) -> AgenticTeamAgent:
    from agent_registry import get_registry

    manifest_id = manifest_agent_id(team_id, agent_name)
    registry = get_registry()
    if registry.get(manifest_id) is None:
        registry.register(build_agent_manifest(team_id, agent_name, summary=summary))
    return AgenticTeamAgent(
        agent_name=agent_name,
        source="generated",
        manifest_id=manifest_id,
    )


def _seed_team_with_agents() -> str:
    store = AgenticTeamStore()
    team = store.create_team(name="Support", description="")
    store.save_team_agents(
        team.team_id,
        [
            _thin_generated(team.team_id, "Triage Agent", summary="Classifies tickets"),
            _thin_generated(team.team_id, "Router Agent", summary="Routes tickets"),
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
