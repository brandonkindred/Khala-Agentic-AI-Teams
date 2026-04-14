"""Tests for team agents pool (store + LLM parsing)."""

from __future__ import annotations

import pytest

from agentic_team_provisioning.assistant.agent import _parse_agents_json
from agentic_team_provisioning.assistant.store import AgenticTeamStore
from agentic_team_provisioning.models import AgenticTeamAgent
from agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


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


def test_save_and_load_team_agents(fake_pg: dict):
    store = AgenticTeamStore()
    team = store.create_team(name="T", description="")

    agents = [
        AgenticTeamAgent(agent_name="Agent A", role="Does A"),
        AgenticTeamAgent(agent_name="Agent B", role="Does B"),
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
        [AgenticTeamAgent(agent_name="Old", role="old role")],
    )
    store.save_team_agents(
        team.team_id,
        [
            AgenticTeamAgent(agent_name="New1", role="r1"),
            AgenticTeamAgent(agent_name="New2", role="r2"),
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
        [AgenticTeamAgent(agent_name="X", role="x role")],
    )
    team_obj = store.get_team(team.team_id)
    assert team_obj is not None
    assert len(team_obj.agents) == 1
    assert team_obj.agents[0].agent_name == "X"
