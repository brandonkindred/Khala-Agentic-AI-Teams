"""Tests that test-chat session endpoints enforce the team_id path tenancy boundary.

Session rows are keyed by ``session_id`` alone in the store; handlers must
reject requests whose path ``team_id`` does not match the session's owning
team (same pattern as message ratings).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _session_row(*, session_id: str, team_id: str, agent_name: str = "Probe Agent") -> dict:
    now = "2026-01-01T00:00:00+00:00"
    return {
        "session_id": session_id,
        "team_id": team_id,
        "agent_name": agent_name,
        "session_name": "",
        "created_at": now,
        "updated_at": now,
    }


def _patch_owner_session(
    monkeypatch: pytest.MonkeyPatch, *, session_id: str = "sess-owner", team_id: str = "team-owner"
) -> dict:
    """Install a chat session owned by ``team_id`` and empty message history."""
    from agent_team_studio.agentic_team_provisioning.api import main

    owner_row = _session_row(session_id=session_id, team_id=team_id)
    monkeypatch.setattr(
        main._test_store, "get_chat_session", lambda sid: owner_row if sid == session_id else None
    )
    monkeypatch.setattr(main._test_store, "list_chat_messages", lambda sid: [])
    return owner_row


def test_get_session_cross_team_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sess-owner"
    _patch_owner_session(monkeypatch, session_id=session_id)

    resp = client.get(f"/teams/team-attacker/test-chat/sessions/{session_id}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"


def test_delete_session_cross_team_returns_404_and_does_not_delete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_team_studio.agentic_team_provisioning.api import main

    session_id = "sess-owner"
    _patch_owner_session(monkeypatch, session_id=session_id)
    deleted: list[str] = []
    monkeypatch.setattr(main._test_store, "delete_chat_session", lambda sid: deleted.append(sid))

    resp = client.delete(f"/teams/team-attacker/test-chat/sessions/{session_id}")

    assert resp.status_code == 404
    assert deleted == []


def test_delete_session_same_team_returns_204(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success path: explicit ``Response(status_code=204)`` must yield empty body."""
    from agent_team_studio.agentic_team_provisioning.api import main

    session_id = "sess-owner"
    _patch_owner_session(monkeypatch, session_id=session_id)
    deleted: list[str] = []
    monkeypatch.setattr(main._test_store, "delete_chat_session", lambda sid: deleted.append(sid))

    resp = client.delete(f"/teams/team-owner/test-chat/sessions/{session_id}")

    assert resp.status_code == 204
    assert resp.content == b""
    assert deleted == [session_id]


def test_get_session_same_team_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_team_studio.agentic_team_provisioning.api import main

    session_id = "sess-owner"
    _patch_owner_session(monkeypatch, session_id=session_id)

    def _missing_agent(*_a, **_k):
        raise HTTPException(status_code=404, detail="Agent not found")

    monkeypatch.setattr(main, "_find_agent_in_roster", _missing_agent)

    resp = client.get(f"/teams/team-owner/test-chat/sessions/{session_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["session_id"] == session_id
    assert body["session"]["team_id"] == "team-owner"
    assert body["messages"] == []
    assert body["suggested_prompts"] == []


def test_rename_session_cross_team_returns_404_and_does_not_rename(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_team_studio.agentic_team_provisioning.api import main

    session_id = "sess-owner"
    _patch_owner_session(monkeypatch, session_id=session_id)
    renamed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main._test_store,
        "rename_chat_session",
        lambda sid, name: renamed.append((sid, name)),
    )

    resp = client.put(
        f"/teams/team-attacker/test-chat/sessions/{session_id}/name",
        json={"session_name": "stolen"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"
    assert renamed == []


def test_export_session_cross_team_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sess-owner"
    _patch_owner_session(monkeypatch, session_id=session_id)

    resp = client.get(f"/teams/team-attacker/test-chat/sessions/{session_id}/export")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"


def test_send_message_cross_team_returns_404_and_does_not_invoke_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_team_studio.agentic_team_provisioning.api import main

    session_id = "sess-owner"
    _patch_owner_session(monkeypatch, session_id=session_id)

    def _boom_build(*_a, **_k):
        raise AssertionError("agent must not be invoked for cross-team session")

    monkeypatch.setattr(main, "_build_test_agent", _boom_build)

    def _fail_persist(*_a, **_k):
        raise AssertionError("create_chat_message must not run for cross-team session")

    monkeypatch.setattr(main._test_store, "create_chat_message", _fail_persist)

    resp = client.post(
        f"/teams/team-attacker/test-chat/sessions/{session_id}/messages",
        json={"content": "hello"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session not found"
