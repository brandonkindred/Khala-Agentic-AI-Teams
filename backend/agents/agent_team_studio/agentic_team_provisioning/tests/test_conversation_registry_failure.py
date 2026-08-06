"""Tests for POST /conversations and POST /conversations/{id}/messages when the
roster save (``_save_agents_from_llm`` -> ``register_team_manifests``) fails
because the agent registry is unavailable.

A registry outage mid-turn must surface as a clear ``503``, not an opaque
``500`` — and the failure happens after the user/assistant chat turn has
already been persisted, so these tests also pin down the resulting
partial-state: the chat turn survives, only the roster write is rolled back.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _boom(*args, **kwargs):
    raise RuntimeError("registry backend down")


def test_create_conversation_registry_failure_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from agent_team_studio.agentic_team_provisioning.api import main

    store = AgenticTeamStore()
    team = store.create_team(name="Ops", description="")

    monkeypatch.setattr(
        main._agent,
        "respond",
        lambda **kwargs: ("Sure, let's design that.", None, [], [{"agent_name": "a", "role": "r"}]),
    )
    monkeypatch.setattr(main, "_save_agents_from_llm", _boom)

    resp = client.post("/conversations", json={"team_id": team.team_id, "initial_message": "hi"})

    assert resp.status_code == 503
    assert "registry" in resp.json()["detail"].lower()

    # Partial state: the chat turn is persisted even though the roster save
    # failed and the route returned 503.
    conversations = store.list_conversations(team.team_id)
    assert len(conversations) == 1
    messages = store.get_messages(conversations[0]["conversation_id"])
    assert [m.role for m in messages] == ["user", "assistant"]


def test_send_message_registry_failure_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from agent_team_studio.agentic_team_provisioning.api import main

    store = AgenticTeamStore()
    team = store.create_team(name="Ops", description="")
    conversation_id = store.create_conversation(team_id=team.team_id)

    monkeypatch.setattr(
        main._agent,
        "respond",
        lambda **kwargs: ("Sure, let's design that.", None, [], [{"agent_name": "a", "role": "r"}]),
    )
    monkeypatch.setattr(main, "_save_agents_from_llm", _boom)

    resp = client.post(f"/conversations/{conversation_id}/messages", json={"message": "hi"})

    assert resp.status_code == 503
    assert "registry" in resp.json()["detail"].lower()

    messages = store.get_messages(conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]
