"""Tests for get_test_chat_session's starter-prompt error handling.

Previously an HTTPException raised while generating starter prompts was
unconditionally swallowed with ``except HTTPException: pass`` — silently
producing an empty prompt list for any failure, including something like a
500 from a downstream registry, with no log signal. The fix only swallows the
genuine 404 "agent not on roster" case (now with a warning log) and
re-raises anything else.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _new_team() -> str:
    return AgenticTeamStore().create_team(name="Ops", description="").team_id


def _stub_session(monkeypatch: pytest.MonkeyPatch, team_id: str, agent_name: str) -> str:
    """Stub the session/message reads _test_store makes (its chat-session SQL
    isn't modeled by the fake Postgres double), without going through the
    unsupported real store path."""
    from agent_team_studio.agentic_team_provisioning.api import main

    session_id = "session-1"
    now = "2026-01-01T00:00:00+00:00"
    session_row = {
        "session_id": session_id,
        "team_id": team_id,
        "agent_name": agent_name,
        "session_name": "",
        "created_at": now,
        "updated_at": now,
    }
    monkeypatch.setattr(main._test_store, "get_chat_session", lambda sid: session_row)
    monkeypatch.setattr(main._test_store, "list_chat_messages", lambda sid: [])
    return session_id


def test_agent_not_on_roster_returns_empty_prompts_and_logs_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    team_id = _new_team()
    session_id = _stub_session(monkeypatch, team_id, "Ghost Agent")

    with caplog.at_level(
        logging.WARNING, logger="agent_team_studio.agentic_team_provisioning.api.main"
    ):
        resp = client.get(f"/teams/{team_id}/test-chat/sessions/{session_id}")

    assert resp.status_code == 200
    assert resp.json()["suggested_prompts"] == []
    assert session_id in caplog.text
    assert "Ghost Agent" in caplog.text


def test_non_404_failure_during_prompt_generation_propagates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A non-404 HTTPException (e.g. a downstream registry failure) is a real
    error and must surface to the caller, not fall back to empty prompts."""
    from agent_team_studio.agentic_team_provisioning.api import main

    team_id = _new_team()
    session_id = _stub_session(monkeypatch, team_id, "Some Agent")

    def _boom(team_id: str, agent_name: str):
        raise HTTPException(status_code=500, detail="registry unavailable")

    monkeypatch.setattr(main, "_find_agent_in_roster", _boom)

    resp = client.get(f"/teams/{team_id}/test-chat/sessions/{session_id}")
    assert resp.status_code == 500


def test_starter_prompts_hydrate_thin_registry_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A thin ``source == "registry"`` roster agent's persona is resolved live
    from its manifest before starter prompts are generated, so the prompts
    reference the real role/skills rather than an empty persona (#5891)."""
    import agent_registry
    from agent_registry.loader import AgentRegistry
    from agent_registry.models import AgentManifest, SourceInfo
    from agent_team_studio.agentic_team_provisioning.api.services.teams import (
        add_agent_from_registry,
    )
    from agent_team_studio.agentic_team_provisioning.models import AddAgentFromRegistryRequest

    reg = AgentRegistry([], {})
    reg.register(
        AgentManifest(
            id="catalog.worker",
            team="catalog",
            name="catalog.worker",
            summary="Plans SEO-aware blog outlines",
            tags=["studio", "seo"],
            cognition=None,
            source=SourceInfo(entrypoint="pkg.mod:Agent"),
        )
    )
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_id = _new_team()
    add_agent_from_registry(team_id, AddAgentFromRegistryRequest(manifest_id="catalog.worker"))
    session_id = _stub_session(monkeypatch, team_id, "catalog.worker")

    resp = client.get(f"/teams/{team_id}/test-chat/sessions/{session_id}")

    assert resp.status_code == 200
    prompts = resp.json()["suggested_prompts"]
    assert any("Plans SEO-aware blog outlines" in p for p in prompts)
