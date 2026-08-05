"""
Agent Console sandbox lifecycle API (issue #265, Phase 3).

All routes are keyed by ``agent_id`` — one sandbox per specialist agent —
rather than by team. The new agent-keyed lifecycle owner lives in
``agent_team_studio.agent_provisioning_team.sandbox``.

- GET    /api/agents/sandboxes                   — list all tracked sandboxes
- GET    /api/agents/sandboxes/metrics           — pool-wide live counters (#302)
- GET    /api/agents/sandboxes/{agent_id}        — status + URL + idle seconds
- POST   /api/agents/sandboxes/{agent_id}/warm   — eager acquire (idempotent)
- DELETE /api/agents/sandboxes/{agent_id}        — teardown
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agent_team_studio.agent_provisioning_team.sandbox import (
    DockerUnavailableError,
    SandboxAcquireFailedError,
    SandboxHandle,
    SandboxMetrics,
    UnknownAgentError,
    list_active,
    metrics,
    status,
)
from agent_team_studio.agent_provisioning_team.sandbox.provisioner import DockerError

# Temporal-aware mutators (durable workflows when Temporal is enabled, direct
# in-process calls otherwise). Read-only routes below stay direct.
from agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch import acquire_sandbox, teardown_sandbox

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
        return await acquire_sandbox(agent_id)
    except UnknownAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (DockerUnavailableError, SandboxAcquireFailedError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{agent_id}", response_model=SandboxHandle)
async def get_status(agent_id: str) -> SandboxHandle:
    try:
        return await status(agent_id)
    except UnknownAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{agent_id}")
async def delete_sandbox(agent_id: str) -> dict[str, str]:
    try:
        await teardown_sandbox(agent_id)
    except DockerError as exc:
        # The only exception teardown()/teardown_sandbox_via_temporal() can
        # realistically raise: stop_container() failing (e.g. daemon
        # unreachable mid-operation). Unlike warm_sandbox, teardown never
        # raises UnknownAgentError (a never-warmed/unknown agent_id is a
        # silent no-op, matching test_teardown_is_idempotent_for_cold_agent)
        # or SandboxAcquireFailedError (acquire-only).
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"agent_id": agent_id, "status": "torn down"}
