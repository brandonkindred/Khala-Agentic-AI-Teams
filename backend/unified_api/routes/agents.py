"""
Agent Console API — catalog, samples, invoke, and run history.

Phase 1 routes:
- GET /api/agents                        — list AgentSummary[] (filter by team/tag/q)
- GET /api/agents/teams                  — list TeamGroup[] for the catalog sidebar
- GET /api/agents/{agent_id}             — full AgentDetail + anatomy markdown if present
- GET /api/agents/{agent_id}/schema/{input|output} — resolved JSON Schema

Phase 2 additions:
- GET /api/agents/{agent_id}/samples              — list golden sample stems
- GET /api/agents/{agent_id}/samples/{name}       — load a sample
- POST /api/agents/{agent_id}/invoke              — warm sandbox + proxy invoke

Phase 3 additions:
- GET /api/agents/{agent_id}/runs?limit&cursor    — paginated run history
- GET /api/agents/runs/{run_id}                   — one run with full payloads
- DELETE /api/agents/runs/{run_id}                — delete one run
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from agent_console import (
    AgentConsoleStorageUnavailable,
    RunRecord,
    RunSummary,
    get_store,
    resolve_author,
)
from agent_console.models import RunCreate
from agent_registry import AgentDetail, AgentSummary, TeamGroup, get_registry
from agent_registry.schema_resolver import SchemaResolutionError, resolve_schema
from agent_sandbox import SandboxStatus, get_manager
from unified_api.config import TEAM_CONFIGS

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


# ---------------------------------------------------------------------------
# Phase 2 — samples
# ---------------------------------------------------------------------------


@router.get("/{agent_id}/samples")
def list_samples(agent_id: str) -> list[str]:
    reg = get_registry()
    if reg.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    return reg.list_samples(agent_id)


@router.get("/{agent_id}/samples/{name}")
def get_sample(agent_id: str, name: str) -> dict[str, Any]:
    reg = get_registry()
    if reg.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    body = reg.get_sample(agent_id, name)
    if body is None:
        raise HTTPException(status_code=404, detail=f"Unknown sample: {agent_id}/{name}")
    return body


# ---------------------------------------------------------------------------
# Phase 2 — invoke
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/invoke")
async def invoke_agent(
    agent_id: str,
    request: Request,
    saved_input_id: str | None = Query(
        default=None,
        description="Optional saved-input id to join this run back to its source.",
    ),
) -> Response:
    manifest = get_registry().get(agent_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    if "requires-live-integration" in manifest.tags:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent {agent_id} requires live integrations and cannot be run in a sandbox. "
                "Invoke it through its team's production API instead."
            ),
        )

    mgr = get_manager()
    try:
        handle = await mgr.ensure_warm(manifest.team)
    except Exception as exc:  # UnknownTeamError or infra problems
        logger.exception("sandbox warm failed for %s", manifest.team)
        raise HTTPException(
            status_code=503,
            detail=f"Sandbox for team {manifest.team!r} is not available: {exc}",
        ) from exc

    if handle.status == SandboxStatus.ERROR:
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Sandbox for {manifest.team} failed to warm.",
                "sandbox_error": handle.error,
            },
        )
    if handle.status != SandboxStatus.WARM or handle.url is None:
        # Warming; let the UI poll.
        return JSONResponse(
            status_code=202,
            headers={"Retry-After": "5"},
            content={
                "status": handle.status,
                "message": "Sandbox is warming. Retry shortly.",
                "sandbox": {"team": manifest.team, "status": handle.status},
            },
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    timeout_s = TEAM_CONFIGS.get(manifest.team).timeout_seconds if manifest.team in TEAM_CONFIGS else 120.0
    target = f"{handle.url}/_agents/{agent_id}/invoke"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            upstream = await client.post(target, json=body)
    except httpx.HTTPError as exc:
        logger.exception("invoke proxy failed %s", target)
        raise HTTPException(status_code=502, detail=f"Sandbox invoke failed: {exc}") from exc

    # Update idle tracker only on a real response (any status — the user still engaged with it).
    await mgr.note_activity(manifest.team)

    content: Any
    try:
        content = upstream.json()
    except ValueError:
        content = {"raw": upstream.text}
    if isinstance(content, dict):
        content.setdefault("sandbox", {"team": manifest.team, "url": handle.url})

    # Best-effort run persistence. Never block the invoke on storage failure.
    _persist_run(
        agent_id=agent_id,
        team=manifest.team,
        saved_input_id=saved_input_id,
        request_body=body,
        upstream_status=upstream.status_code,
        envelope=content,
        sandbox_url=handle.url,
    )

    return JSONResponse(status_code=upstream.status_code, content=content)


# ---------------------------------------------------------------------------
# Phase 3 — run history
# ---------------------------------------------------------------------------


@router.get("/{agent_id}/runs", response_model=list[RunSummary])
def list_runs(
    agent_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(
        default=None,
        description="ISO-8601 `created_at` of the last row from the previous page.",
    ),
) -> list[RunSummary]:
    if get_registry().get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    parsed_cursor: datetime | None = None
    if cursor:
        try:
            parsed_cursor = datetime.fromisoformat(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid cursor: {cursor}") from exc
    try:
        return get_store().list_runs(agent_id, limit=limit, cursor=parsed_cursor)
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: str) -> RunRecord:
    try:
        record = get_store().get_run(run_id)
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return record


@router.delete("/runs/{run_id}")
def delete_run(run_id: str) -> dict[str, str]:
    try:
        deleted = get_store().delete_run(run_id)
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return {"id": run_id, "status": "deleted"}


def _persist_run(
    *,
    agent_id: str,
    team: str,
    saved_input_id: str | None,
    request_body: Any,
    upstream_status: int,
    envelope: Any,
    sandbox_url: str | None,
) -> None:
    """Write one row to agent_console_runs. Swallows all errors by design."""
    try:
        if isinstance(envelope, dict):
            # When the shim returned 422, it wraps the envelope in {"detail": {...}}.
            inner = envelope.get("detail") if upstream_status == 422 else envelope
            if not isinstance(inner, dict):
                inner = {"output": envelope}
        else:
            inner = {"output": envelope}

        status = "ok" if 200 <= upstream_status < 300 and not inner.get("error") else "error"
        duration_ms = int(inner.get("duration_ms") or 0)
        trace_id = str(inner.get("trace_id") or "")
        logs_tail_raw = inner.get("logs_tail") or []
        logs_tail = [str(line) for line in logs_tail_raw if line is not None]
        error_text = inner.get("error") if status == "error" else None
        if status == "error" and not error_text and upstream_status >= 400:
            error_text = f"HTTP {upstream_status}"

        record = RunCreate(
            agent_id=agent_id,
            team=team,
            saved_input_id=saved_input_id,
            input_data=request_body,
            output_data=inner.get("output"),
            error=error_text,
            status=status,  # type: ignore[arg-type]
            duration_ms=duration_ms,
            trace_id=trace_id,
            logs_tail=logs_tail,
            author=resolve_author(),
            sandbox_url=sandbox_url,
        )
        get_store().record_run(record)
    except AgentConsoleStorageUnavailable:
        logger.debug("Agent Console storage unavailable; skipping run persistence")
    except Exception:
        logger.warning("Failed to persist agent_console run", exc_info=True)


def _resolve_or_404(schema_ref: str, *, kind: str) -> dict[str, Any]:
    try:
        return resolve_schema(schema_ref)
    except SchemaResolutionError as exc:
        logger.info("Could not resolve %s schema %r: %s", kind, schema_ref, exc)
        raise HTTPException(
            status_code=404,
            detail=f"Could not resolve {kind} schema {schema_ref!r}: {exc}",
        ) from exc
