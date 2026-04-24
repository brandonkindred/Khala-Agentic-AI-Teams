"""
Agent Console sandbox lifecycle API (issue #265, Phase 3).

All routes are keyed by ``agent_id`` — one sandbox per specialist agent —
rather than by team. The new agent-keyed lifecycle owner lives in
``agent_provisioning_team.sandbox``.

- GET    /api/agents/sandboxes                   — list all tracked sandboxes
- GET    /api/agents/sandboxes/metrics           — pool-wide live counters (#302)
- GET    /api/agents/sandboxes/{agent_id}        — status + URL + idle seconds
- POST   /api/agents/sandboxes/{agent_id}/warm   — eager acquire (idempotent)
- DELETE /api/agents/sandboxes/{agent_id}        — teardown
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agent_provisioning_team.sandbox import (
    SandboxHandle,
    SandboxMetrics,
    UnknownAgentError,
    acquire,
    list_active,
    metrics,
    status,
    teardown,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents/sandboxes", tags=["agent-console"])


@router.get("", response_model=list[SandboxHandle])
@router.get("/", response_model=list[SandboxHandle])
async def list_sandboxes() -> list[SandboxHandle]:
    return await list_active()


# Registered BEFORE /{agent_id} so FastAPI doesn't capture "metrics" as an id.
@router.get("/metrics", response_model=SandboxMetrics)
async def sandbox_metrics() -> SandboxMetrics:
    return await metrics()


@router.post("/{agent_id}/warm", response_model=SandboxHandle)
async def warm_sandbox(agent_id: str) -> SandboxHandle:
    try:
        return await acquire(agent_id)
    except UnknownAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{agent_id}", response_model=SandboxHandle)
async def get_status(agent_id: str) -> SandboxHandle:
    try:
        return await status(agent_id)
    except UnknownAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{agent_id}")
async def delete_sandbox(agent_id: str) -> dict[str, str]:
    await teardown(agent_id)
    return {"agent_id": agent_id, "status": "torn down"}
