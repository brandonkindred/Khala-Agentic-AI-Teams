"""
Read-only Agent Console catalog API.

- GET /api/agents                        — list AgentSummary[] (filter by team/tag/q)
- GET /api/agents/teams                  — list TeamGroup[] for the catalog sidebar
- GET /api/agents/{agent_id}             — full AgentDetail + anatomy markdown if present
- GET /api/agents/{agent_id}/schema/input  — resolved JSON Schema (404 if unavailable)
- GET /api/agents/{agent_id}/schema/output — resolved JSON Schema (404 if unavailable)

Invocation is out of scope for Phase 1 (Catalog only).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from agent_registry import AgentDetail, AgentSummary, TeamGroup, get_registry
from agent_registry.schema_resolver import SchemaResolutionError, resolve_schema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agent-console"])


@router.get("", response_model=list[AgentSummary])
@router.get("/", response_model=list[AgentSummary])
def list_agents(
    team: str | None = Query(default=None, description="Filter by team key."),
    tag: str | None = Query(default=None, description="Filter by tag (exact match)."),
    q: str | None = Query(default=None, description="Full-text query over name/summary/tags."),
) -> list[AgentSummary]:
    return get_registry().search(team=team, tag=tag, q=q)


@router.get("/teams", response_model=list[TeamGroup])
def list_teams() -> list[TeamGroup]:
    return get_registry().teams()


@router.get("/{agent_id}", response_model=AgentDetail)
def get_agent(agent_id: str) -> AgentDetail:
    detail = get_registry().detail(agent_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    return detail


@router.get("/{agent_id}/schema/input")
def get_input_schema(agent_id: str) -> dict[str, Any]:
    manifest = get_registry().get(agent_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    if not (manifest.inputs and manifest.inputs.schema_ref):
        raise HTTPException(status_code=404, detail="Agent has no input schema_ref configured.")
    return _resolve_or_404(manifest.inputs.schema_ref, kind="input")


@router.get("/{agent_id}/schema/output")
def get_output_schema(agent_id: str) -> dict[str, Any]:
    manifest = get_registry().get(agent_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    if not (manifest.outputs and manifest.outputs.schema_ref):
        raise HTTPException(status_code=404, detail="Agent has no output schema_ref configured.")
    return _resolve_or_404(manifest.outputs.schema_ref, kind="output")


def _resolve_or_404(schema_ref: str, *, kind: str) -> dict[str, Any]:
    try:
        return resolve_schema(schema_ref)
    except SchemaResolutionError as exc:
        logger.info("Could not resolve %s schema %r: %s", kind, schema_ref, exc)
        raise HTTPException(
            status_code=404,
            detail=f"Could not resolve {kind} schema {schema_ref!r}: {exc}",
        ) from exc
