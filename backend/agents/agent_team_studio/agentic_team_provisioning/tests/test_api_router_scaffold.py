"""Smoke: teams/conversations/testing routers exist, mount, and resolve through the hub."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute

from agent_team_studio.agentic_team_provisioning.models import (
    CreateConversationRequest,
    CreateTeamRequest,
    SendTestChatMessageRequest,
    SetTeamModeRequest,
    TeamMode,
)

# All extracted paths — catches a dropped include_router while hub aliases remain,
# and catches a single omitted handler inside a mounted router.
_EXTRACTED_ROUTE_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/teams"),
        ("GET", "/teams"),
        ("GET", "/teams/{team_id}"),
        ("GET", "/teams/{team_id}/agents"),
        ("POST", "/teams/{team_id}/agents/from-registry"),
        ("POST", "/conversations"),
        ("POST", "/conversations/{conversation_id}/messages"),
        ("PUT", "/conversations/{conversation_id}/process"),
        ("GET", "/teams/{team_id}/conversations"),
        ("PUT", "/teams/{team_id}/mode"),
        ("POST", "/teams/{team_id}/test-chat/sessions"),
        ("GET", "/teams/{team_id}/test-chat/sessions"),
        ("GET", "/teams/{team_id}/test-chat/sessions/{session_id}"),
        ("PUT", "/teams/{team_id}/test-chat/sessions/{session_id}/name"),
        ("DELETE", "/teams/{team_id}/test-chat/sessions/{session_id}"),
        ("POST", "/teams/{team_id}/test-chat/sessions/{session_id}/messages"),
        ("GET", "/teams/{team_id}/test-chat/sessions/{session_id}/export"),
        ("PUT", "/teams/{team_id}/test-chat/messages/{message_id}/rating"),
        ("GET", "/teams/{team_id}/test-chat/quality-scores"),
        ("POST", "/teams/{team_id}/test-pipeline/runs"),
        ("GET", "/teams/{team_id}/test-pipeline/runs"),
        ("GET", "/teams/{team_id}/test-pipeline/runs/{run_id}"),
        ("POST", "/teams/{team_id}/test-pipeline/runs/{run_id}/input"),
        ("POST", "/teams/{team_id}/test-pipeline/runs/{run_id}/cancel"),
    }
)


def _app_route_keys(app) -> set[tuple[str, str]]:
    """Collect (METHOD, path) pairs registered on a FastAPI app.

    Preconditions: ``app`` is a FastAPI application with ``app.routes`` populated.
    Postconditions: returns one (method, path) entry per APIRoute method; non-API
        routes (Mount, WebSocket) are omitted.
    """
    keys: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            keys.add((method, route.path))
    return keys


def test_teams_and_conversations_routers_importable() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes import conversations, teams

    assert isinstance(teams.router, APIRouter)
    assert isinstance(conversations.router, APIRouter)


def test_testing_router_importable() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes import testing

    assert isinstance(testing.router, APIRouter)


def test_main_exposes_testing_router_marker() -> None:
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.routes import testing

    assert main_mod._testing_router is testing.router


def test_main_exposes_mounted_router_markers() -> None:
    """main keeps explicit references so we can assert include_router ran."""
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.routes import conversations, teams, testing

    assert main_mod._teams_router is teams.router
    assert main_mod._conversations_router is conversations.router
    assert main_mod._testing_router is testing.router
    paths = {getattr(r, "path", None) for r in main_mod.app.routes if isinstance(r, APIRoute)}
    assert "/health" in paths


def test_extracted_teams_and_conversations_paths_are_mounted() -> None:
    """include_router must register extracted paths on the app (not just hub aliases).

    Preconditions: ``main.app`` has finished module import (routers mounted last).
    Postconditions: every key in ``_EXTRACTED_ROUTE_KEYS`` appears on ``app.routes``.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod

    registered = _app_route_keys(main_mod.app)
    missing = _EXTRACTED_ROUTE_KEYS - registered
    assert not missing, f"extracted routes not mounted on app: {sorted(missing)}"


def test_main_exposes_test_agent_hub_aliases() -> None:
    """Hub must bind ``_build_test_agent`` / ``_call_test_agent`` as real import aliases.

    Monkeypatch alone cannot prove this: ``setattr`` creates missing attributes.
    Preconditions: ``main`` finished module import.
    Postconditions: both names are callable on ``main`` without prior setattr.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod

    assert callable(getattr(main_mod, "_build_test_agent", None))
    assert callable(getattr(main_mod, "_call_test_agent", None))


def test_teams_service_create_team_reads_store_from_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hub dereference: patching ``main._store`` must be visible to the teams service."""
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.services import teams as teams_svc

    class _BoomStore:
        def create_team(self, **_kwargs):
            raise RuntimeError("hub-store-hit")

    monkeypatch.setattr(main_mod, "_store", _BoomStore())
    with pytest.raises(RuntimeError, match="hub-store-hit"):
        teams_svc.create_team(CreateTeamRequest(name="wiring-probe"))


def test_conversations_service_reads_store_from_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hub dereference: patching ``main._store`` must be visible to conversations."""
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.services import conversations as conv_svc

    class _BoomStore:
        def get_team(self, _team_id: str):
            raise RuntimeError("hub-store-hit")

    monkeypatch.setattr(main_mod, "_store", _BoomStore())
    with pytest.raises(RuntimeError, match="hub-store-hit"):
        conv_svc.create_conversation(CreateConversationRequest(team_id="any"))


def test_testing_service_reads_test_store_from_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hub dereference: patching ``main._test_store`` must be visible to testing."""
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.services import testing as testing_svc

    class _Team:
        pass

    class _Boom:
        def set_team_mode(self, *_a, **_k):
            raise RuntimeError("hub-test-store-hit")

    monkeypatch.setattr(
        main_mod, "_store", type("Store", (), {"get_team": lambda self, tid: _Team()})()
    )
    monkeypatch.setattr(main_mod, "_test_store", _Boom())
    with pytest.raises(RuntimeError, match="hub-test-store-hit"):
        testing_svc.set_team_mode(team_id="t1", req=SetTeamModeRequest(mode=TeamMode.TESTING))


def test_testing_service_send_message_uses_hub_build_test_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hub dereference: ``send_test_chat_message`` must call ``main._build_test_agent``.

    Preconditions: session/roster fakes succeed so the agent-invocation path runs.
    Postconditions: a boom from ``main._build_test_agent`` surfaces as HTTP 502
        (wrapped by the service), proving the hub alias was dereferenced.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.services import testing as testing_svc
    from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent

    session_id = "sess-wiring"
    team_id = "team-wiring"
    session_row = {
        "session_id": session_id,
        "team_id": team_id,
        "agent_name": "Probe Agent",
        "session_name": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    class _Store:
        def get_chat_session(self, sid: str):
            return session_row if sid == session_id else None

        def list_chat_messages(self, sid: str):
            return []

    monkeypatch.setattr(main_mod, "_test_store", _Store())
    monkeypatch.setattr(
        main_mod,
        "_find_agent_in_roster",
        lambda tid, name: AgenticTeamAgent(agent_name=name, role="probe"),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("hub-build-test-agent-hit")

    monkeypatch.setattr(main_mod, "_build_test_agent", _boom)

    with pytest.raises(HTTPException) as exc_info:
        testing_svc.send_test_chat_message(
            team_id, session_id, SendTestChatMessageRequest(content="hi")
        )
    assert exc_info.value.status_code == 502
