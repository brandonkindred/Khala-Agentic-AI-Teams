"""Smoke: teams/conversations routers exist, mount, and resolve through the hub."""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

from agent_team_studio.agentic_team_provisioning.models import (
    CreateConversationRequest,
    CreateTeamRequest,
)

# Representative extracted paths — enough to catch a dropped include_router while
# hub aliases (_teams_router / _conversations_router) remain assigned.
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
    from agent_team_studio.agentic_team_provisioning.api.routes import conversations, teams

    assert main_mod._teams_router is teams.router
    assert main_mod._conversations_router is conversations.router
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
