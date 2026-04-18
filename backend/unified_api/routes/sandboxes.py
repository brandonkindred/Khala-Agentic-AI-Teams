"""
Agent Console sandbox lifecycle API.

- POST   /api/agents/sandboxes/{team} — ensure warm (idempotent)
- GET    /api/agents/sandboxes/{team} — status + URL + last-used + idle seconds
- DELETE /api/agents/sandboxes/{team} — teardown
- GET    /api/agents/sandboxes         — list all warm sandboxes
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agent_sandbox import SandboxHandle, get_manager
from agent_sandbox.manager import UnknownTeamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents/sandboxes", tags=["agent-console"])


@router.get("", response_model=list[SandboxHandle])
@router.get("/", response_model=list[SandboxHandle])
async def list_warm_sandboxes() -> list[SandboxHandle]:
    return await get_manager().list_warm()


@router.post("/{team}", response_model=SandboxHandle)
async def ensure_warm(team: str) -> SandboxHandle:
    try:
        return await get_manager().ensure_warm(team)
    except UnknownTeamError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{team}", response_model=SandboxHandle)
async def get_status(team: str) -> SandboxHandle:
    try:
        return await get_manager().status(team)
    except UnknownTeamError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{team}")
async def teardown(team: str) -> dict[str, str]:
    try:
        await get_manager().teardown(team)
    except UnknownTeamError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"team": team, "status": "torn down"}
