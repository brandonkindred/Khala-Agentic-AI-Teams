"""Tests for team agents pool (store + LLM parsing)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_team_studio.agentic_team_provisioning.assistant.agent import _parse_agents_json
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.manifest_generation import manifest_agent_id
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


def _thin_agent(team_id: str, agent_name: str) -> AgenticTeamAgent:
    return AgenticTeamAgent(
        agent_name=agent_name,
        manifest_id=manifest_agent_id(team_id, agent_name),
    )


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_parse_agents_json_valid():
    text = 'Here are the agents:\n```agents\n[{"agent_name":"A1","role":"does stuff"}]\n```\nDone.'
    result = _parse_agents_json(text)
    assert result == [{"agent_name": "A1", "role": "does stuff"}]


def test_parse_agents_json_missing():
    assert _parse_agents_json("No agents block here.") is None


def test_parse_agents_json_bad_json():
    text = "```agents\nnot valid json\n```"
    assert _parse_agents_json(text) is None


def test_list_team_agents_migrates_fat_row(fake_pg: dict, monkeypatch: pytest.MonkeyPatch):
    store = AgenticTeamStore()
    team = store.create_team(name="T-migrate", description="")
    team_id = team.team_id
    fat = {
        "agent_name": "Writer",
        "role": "Writes docs",
        "skills": ["seo"],
        "capabilities": [],
        "tools": [],
        "expertise": [],
        "source": "generated",
        "manifest_id": None,
    }
    now = datetime.now(tz=timezone.utc)
    fake_pg["team_agents"][(team_id, "Writer")] = {
        "team_id": team_id,
        "agent_name": "Writer",
        "data_json": fat,
        "created_at": now,
        "updated_at": now,
    }
    expected_id = manifest_agent_id(team_id, "Writer")

    class _Reg:
        def __init__(self) -> None:
            self._m: dict = {}

        def get(self, agent_id: str):
            return self._m.get(agent_id)

        def register(self, manifest, source_path=None, *, require_persist: bool = False):
            self._m[manifest.id] = manifest

    reg = _Reg()
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.roster_resolve.get_registry",
        lambda: reg,
    )
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)

    loaded = store.list_team_agents(team_id)
    assert len(loaded) == 1
    assert loaded[0].agent_name == "Writer"
    assert loaded[0].manifest_id == expected_id
    assert loaded[0].model_dump(mode="json") == {
        "agent_name": "Writer",
        "source": "generated",
        "manifest_id": expected_id,
    }
    stored = fake_pg["team_agents"][(team_id, "Writer")]["data_json"]
    assert stored == loaded[0].model_dump(mode="json")

    loaded_again = store.list_team_agents(team_id)
    assert loaded_again[0].manifest_id == expected_id
    assert fake_pg["team_agents"][(team_id, "Writer")]["data_json"] == stored


def test_save_and_load_team_agents(fake_pg: dict):
    store = AgenticTeamStore()
    team = store.create_team(name="T", description="")

    agents = [
        _thin_agent(team.team_id, "Agent A"),
        _thin_agent(team.team_id, "Agent B"),
    ]
    store.save_team_agents(team.team_id, agents)

    loaded = store.list_team_agents(team.team_id)
    assert len(loaded) == 2
    assert loaded[0].agent_name == "Agent A"
    assert loaded[1].agent_name == "Agent B"


def test_save_team_agents_replaces(fake_pg: dict):
    store = AgenticTeamStore()
    team = store.create_team(name="T2", description="")

    store.save_team_agents(
        team.team_id,
        [_thin_agent(team.team_id, "Old")],
    )
    store.save_team_agents(
        team.team_id,
        [
            _thin_agent(team.team_id, "New1"),
            _thin_agent(team.team_id, "New2"),
        ],
    )
    loaded = store.list_team_agents(team.team_id)
    names = [a.agent_name for a in loaded]
    assert "Old" not in names
    assert "New1" in names
    assert "New2" in names


def test_get_team_includes_agents(fake_pg: dict):
    store = AgenticTeamStore()
    team = store.create_team(name="T3", description="")
    store.save_team_agents(
        team.team_id,
        [_thin_agent(team.team_id, "X")],
    )
    team_obj = store.get_team(team.team_id)
    assert team_obj is not None
    assert len(team_obj.agents) == 1
    assert team_obj.agents[0].agent_name == "X"
