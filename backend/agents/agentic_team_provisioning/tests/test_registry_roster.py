"""Tests for the §5.3 registry→roster bridge.

Covers ``POST /teams/{id}/agents/from-registry`` (project a registered
``AgentManifest`` into the roster) and ``DELETE /teams/{id}/agents/{name}``,
plus the projection's effect on roster validation. The agent registry is faked
so no manifests are loaded from disk.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo
from agentic_team_provisioning.assistant.store import AgenticTeamStore
from agentic_team_provisioning.tests._fake_postgres import install_fake_postgres

_SOURCE = SourceInfo(entrypoint="pkg.mod:Agent")

# A fully-specified manifest (tags + cognition tools) and a bare one (no summary,
# no cognition) to exercise both projection branches.
_PLANNER = AgentManifest(
    id="blogging.planner",
    team="blogging",
    name="blogging.planner",
    summary="Plans SEO-aware blog outlines",
    tags=["studio", "seo"],
    cognition=CognitionSpec(tools=["web.search", "draft"]),
    source=_SOURCE,
)
_BARE = AgentManifest(
    id="misc.bare",
    team="misc",
    name="misc.bare",
    summary="",
    tags=["studio"],
    cognition=None,
    source=_SOURCE,
)


class _FakeRegistry:
    """The slice of ``AgentRegistry`` the from-registry route uses."""

    def __init__(self, manifests: list[AgentManifest]) -> None:
        self._by_id = {m.id: m for m in manifests}

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._by_id.get(agent_id)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_postgres(monkeypatch)
    # The route resolves the registry via ``from agent_registry import get_registry``;
    # patch the package attribute so the call picks up the fake.
    monkeypatch.setattr("agent_registry.get_registry", lambda: _FakeRegistry([_PLANNER, _BARE]))
    from agentic_team_provisioning.api.main import app

    return TestClient(app)


def _new_team() -> str:
    return AgenticTeamStore().create_team(name="Growth Pod", description="").team_id


def test_from_registry_projects_and_persists(client: TestClient) -> None:
    team_id = _new_team()

    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_name"] == "blogging.planner"
    assert body["role"] == "Plans SEO-aware blog outlines"
    assert body["skills"] == ["studio", "seo"]  # from manifest tags
    assert body["tools"] == ["web.search", "draft"]  # from cognition.tools
    assert body["source"] == "registry"
    assert body["manifest_id"] == "blogging.planner"

    # Persisted on the roster.
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert [a["agent_name"] for a in roster] == ["blogging.planner"]
    assert roster[0]["source"] == "registry"


def test_from_registry_bare_manifest_falls_back(client: TestClient) -> None:
    """No summary → role falls back to name; no cognition → empty tools."""
    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "misc.bare"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "misc.bare"
    assert body["tools"] == []
    assert body["skills"] == ["studio"]


def test_from_registry_agent_passes_roster_validation(client: TestClient) -> None:
    """A projected registry agent fills enough fields to pass depth validation."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    validation = client.get(f"/teams/{team_id}/roster/validation").json()
    assert validation["is_fully_staffed"] is True
    assert validation["gaps"] == []


def test_from_registry_is_idempotent_by_name(client: TestClient) -> None:
    """Re-adding the same manifest updates in place rather than duplicating."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    roster = client.get(f"/teams/{team_id}/agents").json()
    assert len(roster) == 1


def test_from_registry_unknown_manifest_404(client: TestClient) -> None:
    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "nope"})
    assert resp.status_code == 404


def test_from_registry_unknown_team_404(client: TestClient) -> None:
    resp = client.post(
        "/teams/missing/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 404


def test_delete_removes_agent(client: TestClient) -> None:
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    resp = client.delete(f"/teams/{team_id}/agents/blogging.planner")
    assert resp.status_code == 204
    assert client.get(f"/teams/{team_id}/agents").json() == []


def test_delete_unknown_agent_404(client: TestClient) -> None:
    team_id = _new_team()
    resp = client.delete(f"/teams/{team_id}/agents/ghost")
    assert resp.status_code == 404


def test_delete_unknown_team_404(client: TestClient) -> None:
    resp = client.delete("/teams/missing/agents/whoever")
    assert resp.status_code == 404


def test_delete_only_removes_the_named_agent(client: TestClient) -> None:
    """Single-agent delete must not disturb the rest of the roster."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "misc.bare"})

    client.delete(f"/teams/{team_id}/agents/blogging.planner")
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert [a["agent_name"] for a in roster] == ["misc.bare"]
