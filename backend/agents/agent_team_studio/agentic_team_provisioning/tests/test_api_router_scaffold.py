"""Smoke: teams/conversations router packages exist and main mounts them."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.routing import APIRoute


def test_teams_and_conversations_routers_importable() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes import conversations, teams

    assert isinstance(teams.router, APIRouter)
    assert isinstance(conversations.router, APIRouter)


def test_main_exposes_mounted_router_markers() -> None:
    """main keeps explicit references so we can assert include_router ran."""
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.routes import conversations, teams

    assert main_mod._teams_router is teams.router
    assert main_mod._conversations_router is conversations.router
    paths = {getattr(r, "path", None) for r in main_mod.app.routes if isinstance(r, APIRoute)}
    assert "/health" in paths
