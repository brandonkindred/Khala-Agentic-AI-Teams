"""Tests for send_test_chat_message's turn atomicity on agent-invocation failure.

Previously the user message was persisted before invoking the agent. If the
agent call raised, the user message was left orphaned with no assistant
response, and the endpoint returned a generic 500. The fix defers persistence
of both messages until after a successful agent call, and returns 502 on
failure.
"""

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
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _seed_team_with_agent() -> tuple[str, str]:
    store = AgenticTeamStore()
    team = store.create_team(name="Support", description="")
    agent_name = "Triage Agent"
    from agent_platform.registry import get_registry

    manifest_id = manifest_agent_id(team.team_id, agent_name)
    registry = get_registry()
    if registry.get(manifest_id) is None:
        registry.register(
            build_agent_manifest(team.team_id, agent_name, summary="Classifies tickets")
        )
    store.save_team_agents(
        team.team_id,
        [
            AgenticTeamAgent(
                agent_name=agent_name,
                source="generated",
                manifest_id=manifest_id,
            )
        ],
    )
    return team.team_id, agent_name


def _fake_session_row(team_id: str, agent_name: str, session_id: str) -> dict:
    now = "2026-01-01T00:00:00+00:00"
    return {
        "session_id": session_id,
        "team_id": team_id,
        "agent_name": agent_name,
        "session_name": "",
        "created_at": now,
        "updated_at": now,
    }


def test_send_message_agent_failure_returns_502_and_persists_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from agent_team_studio.agentic_team_provisioning.api import main

    team_id, agent_name = _seed_team_with_agent()
    session_id = "session-1"
    session_row = _fake_session_row(team_id, agent_name, session_id)

    monkeypatch.setattr(main._test_store, "get_chat_session", lambda sid: session_row)
    monkeypatch.setattr(main._test_store, "list_chat_messages", lambda sid: [])

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("create_chat_message must not be called on agent failure")

    monkeypatch.setattr(main._test_store, "create_chat_message", _fail_if_called)

    monkeypatch.setattr(main, "_build_test_agent", lambda *args, **kwargs: object())

    def _raise(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(main, "_call_test_agent", _raise)

    resp = client.post(
        f"/teams/{team_id}/test-chat/sessions/{session_id}/messages",
        json={"content": "Hello"},
    )

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Agent invocation failed"


def test_send_message_success_persists_both_messages_together(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from agent_team_studio.agentic_team_provisioning.api import main

    team_id, agent_name = _seed_team_with_agent()
    session_id = "session-2"
    session_row = _fake_session_row(team_id, agent_name, session_id)

    monkeypatch.setattr(main._test_store, "get_chat_session", lambda sid: session_row)
    monkeypatch.setattr(main._test_store, "list_chat_messages", lambda sid: [])

    created: list[tuple[str, str]] = []

    def _record_create(message_id, session_id_arg, role, content):
        created.append((role, content))
        return {
            "message_id": message_id,
            "session_id": session_id_arg,
            "role": role,
            "content": content,
            "rating": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(main._test_store, "create_chat_message", _record_create)
    monkeypatch.setattr(main, "_build_test_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "_call_test_agent", lambda *args, **kwargs: "Hi there")

    resp = client.post(
        f"/teams/{team_id}/test-chat/sessions/{session_id}/messages",
        json={"content": "Hello"},
    )

    assert resp.status_code == 200
    assert created == [("user", "Hello"), ("assistant", "Hi there")]
