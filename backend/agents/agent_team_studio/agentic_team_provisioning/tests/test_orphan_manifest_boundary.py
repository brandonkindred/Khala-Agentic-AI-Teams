"""Boundary handling when a roster ref's manifest_id no longer resolves."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.api.main import enrich_roster_agent
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.models import (
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessStep,
    ProcessStepAgent,
)
from agent_team_studio.agentic_team_provisioning.roster_resolve import EMPTY_ROSTER_PERSONA
from agent_team_studio.agentic_team_provisioning.roster_validation import validate_roster
from agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner import PipelineRunner
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres

_ORPHAN_MANIFEST_ID = "registry.orphan.agent"


def _orphan_agent(name: str = "orphan.agent") -> AgenticTeamAgent:
    return AgenticTeamAgent(
        agent_name=name,
        source="registry",
        manifest_id=_ORPHAN_MANIFEST_ID,
    )


class _EmptyRegistry:
    def get(self, agent_id: str, *, conn=None) -> None:
        return None


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_registry.get_registry", lambda: _EmptyRegistry())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, empty_registry: None) -> TestClient:
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return TestClient(app)


def test_enrich_roster_agent_soft_enriches_orphan(empty_registry: None) -> None:
    agent = _orphan_agent()
    enriched = enrich_roster_agent(agent)
    assert enriched.agent_name == agent.agent_name
    assert enriched.source == agent.source
    assert enriched.manifest_id == agent.manifest_id
    assert enriched.role == ""
    assert enriched.skills == []
    assert enriched.capabilities == []
    assert enriched.tools == []
    assert enriched.expertise == []


def test_get_agents_returns_200_with_orphan(client: TestClient) -> None:
    team_id = AgenticTeamStore().create_team(name="Orphan Pod", description="").team_id
    AgenticTeamStore().save_team_agents(team_id, [_orphan_agent()])

    resp = client.get(f"/teams/{team_id}/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["agent_name"] == "orphan.agent"
    assert row["manifest_id"] == _ORPHAN_MANIFEST_ID
    assert row["role"] == ""
    assert row["skills"] == []


def test_get_team_soft_enriches_orphan_agents(client: TestClient) -> None:
    """GET /teams/{id} must enrich nested agents (same soft-orphan contract as list)."""
    team_id = AgenticTeamStore().create_team(name="Detail Pod", description="").team_id
    AgenticTeamStore().save_team_agents(team_id, [_orphan_agent()])

    resp = client.get(f"/teams/{team_id}")
    assert resp.status_code == 200
    agents = resp.json()["team"]["agents"]
    assert len(agents) == 1
    assert agents[0]["agent_name"] == "orphan.agent"
    assert agents[0]["manifest_id"] == _ORPHAN_MANIFEST_ID
    assert agents[0]["role"] == ""
    assert agents[0]["skills"] == []
    assert "role" in agents[0]


def test_manifests_endpoint_omits_orphan_generated(client: TestClient) -> None:
    """Generated orphan refs must not advertise a fabricated unregistered Manifest."""
    team_id = AgenticTeamStore().create_team(name="Gen Orphan", description="").team_id
    AgenticTeamStore().save_team_agents(
        team_id,
        [
            AgenticTeamAgent(
                agent_name="Writer",
                source="generated",
                manifest_id="agentic_team_provisioning.missing.writer",
            )
        ],
    )

    resp = client.get(f"/teams/{team_id}/agents/manifests")
    assert resp.status_code == 200
    assert resp.json()["manifests"] == []


def test_validate_roster_reports_missing_manifest(empty_registry: None) -> None:
    from agent_team_studio.agentic_team_provisioning.models import (
        AgenticTeam,
        ProcessOutput,
        ProcessStatus,
        ProcessTrigger,
        TriggerType,
    )

    agent = _orphan_agent()
    proc = ProcessDefinition(
        process_id="p1",
        name="Flow",
        trigger=ProcessTrigger(trigger_type=TriggerType.MESSAGE, description="go"),
        steps=[
            ProcessStep(
                step_id="s1",
                name="Step",
                agents=[ProcessStepAgent(agent_name=agent.agent_name, role="do")],
            )
        ],
        output=ProcessOutput(description="done", destination="out"),
        status=ProcessStatus.DRAFT,
    )
    team = AgenticTeam(team_id="t1", name="T", agents=[agent], processes=[proc])

    result = validate_roster(team)
    assert result.is_fully_staffed is False
    assert any(g.category == "missing_manifest" for g in result.gaps)


def test_recommend_skips_orphan_agents(client: TestClient) -> None:
    team_id = AgenticTeamStore().create_team(name="Rec Pod", description="").team_id
    AgenticTeamStore().save_team_agents(team_id, [_orphan_agent()])

    process = ProcessDefinition(
        process_id="p-orphan",
        name="SEO research",
        description="Find seo keywords for the outline",
        steps=[ProcessStep(step_id="s1", name="Keyword seo scan", description="")],
    )
    AgenticTeamStore().save_process(team_id, process)

    resp = client.post("/processes/p-orphan/steps/s1/recommend-agents")
    assert resp.status_code == 200
    assert resp.json()["recommended_agents"] == []


def test_pipeline_run_agent_fail_closed_on_orphan(empty_registry: None) -> None:
    agent = _orphan_agent()
    with pytest.raises(LookupError):
        PipelineRunner._run_agent(agent, "prompt")


def test_send_test_chat_fail_closed_on_orphan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_team_studio.agentic_team_provisioning.api import main

    team_id = AgenticTeamStore().create_team(name="Chat Pod", description="").team_id
    agent = _orphan_agent("Triage Agent")
    AgenticTeamStore().save_team_agents(team_id, [agent])

    session_id = "session-orphan"
    session_row = {
        "session_id": session_id,
        "team_id": team_id,
        "agent_name": agent.agent_name,
        "session_name": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(main._test_store, "get_chat_session", lambda sid: session_row)
    monkeypatch.setattr(main._test_store, "list_chat_messages", lambda sid: [])

    resp = client.post(
        f"/teams/{team_id}/test-chat/sessions/{session_id}/messages",
        json={"content": "hello"},
    )
    assert resp.status_code == 502


def test_empty_roster_persona_is_all_blank() -> None:
    assert EMPTY_ROSTER_PERSONA.role == ""
    assert EMPTY_ROSTER_PERSONA.skills == []
    assert EMPTY_ROSTER_PERSONA.capabilities == []
    assert EMPTY_ROSTER_PERSONA.tools == []
    assert EMPTY_ROSTER_PERSONA.expertise == []
