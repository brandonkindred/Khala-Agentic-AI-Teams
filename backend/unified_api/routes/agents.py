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

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from agent_cognition.invoke_gate import (
    GateOutcomeKind,
    UnmappedGateOutcomeError,
    abandon_invoke,
    finalize_invoke,
    outcome_as_finalized,
    prepare_invoke,
)
from agent_cognition.tools.envelope import ENVELOPE_MARKER
from agent_console import (
    AgentConsoleStorageUnavailable,
    RunRecord,
    RunSummary,
    get_store,
    resolve_author,
)
from agent_console.models import RunCreate
from agent_platform.registry import AgentDetail, AgentSummary, TeamGroup, get_registry
from agent_platform.registry.schema_resolver import SchemaResolutionError, resolve_schema
from agent_team_studio.agent_provisioning_team.sandbox import (
    DockerUnavailableError,
    SandboxStatus,
    note_activity,
)
from agent_team_studio.agent_provisioning_team.sandbox.state import COLD_START_LOG_PREFIX
from shared.agent_invoke.limits import (
    RESPONSE_ENVELOPE_OVERHEAD_BYTES,
    max_output_bytes,
    max_payload_bytes,
    max_writeback_bytes,
    read_json_capped,
)
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
    """List every team present in the merged agent registry.

    Preconditions: none.
    Postconditions: returns one ``TeamGroup`` per distinct team key across all
        merged manifests (static + dynamic), sorted by ``display_name``
        case-insensitively; each group's ``tags`` is the sorted union of its
        agents' tags and ``agent_count`` is the number of manifests for that
        team. Returns an empty list when the registry has no manifests.
    """
    return get_registry().teams()


@router.get("/{agent_id}", response_model=AgentDetail)
def get_agent(agent_id: str) -> AgentDetail:
    detail = get_registry().detail(agent_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    return detail


@router.get("/{agent_id}/schema/input")
def get_input_schema(agent_id: str) -> dict[str, Any]:
    """Return the agent's input JSON Schema.

    Resolution order (inline takes precedence over the dotted ref):
        1. An authored ``inputs.inline_schema`` (present ⇒ returned verbatim, even
           ``{}``) — the schema the user wrote in Agent Studio.
        2. Else a dotted ``inputs.schema_ref`` resolved to a JSON Schema.

    404 when the agent id is unknown, or when the agent advertises neither an
    inline schema nor a resolvable ``schema_ref``.
    """
    manifest = get_registry().get(agent_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    if manifest.inputs and manifest.inputs.inline_schema is not None:
        return manifest.inputs.inline_schema
    if not (manifest.inputs and manifest.inputs.schema_ref):
        raise HTTPException(status_code=404, detail="Agent has no input schema configured.")
    return _resolve_or_404(manifest.inputs.schema_ref, kind="input")


@router.get("/{agent_id}/schema/output")
def get_output_schema(agent_id: str) -> dict[str, Any]:
    """Return the agent's output JSON Schema.

    Mirrors :func:`get_input_schema`: an authored ``outputs.inline_schema`` is
    returned verbatim when present (including ``{}``); otherwise a dotted
    ``outputs.schema_ref`` is resolved. 404 when the agent id is unknown or it
    advertises neither an inline schema nor a resolvable ``schema_ref``.
    """
    manifest = get_registry().get(agent_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    if manifest.outputs and manifest.outputs.inline_schema is not None:
        return manifest.outputs.inline_schema
    if not (manifest.outputs and manifest.outputs.schema_ref):
        raise HTTPException(status_code=404, detail="Agent has no output schema configured.")
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


async def acquire(agent_id: str):
    """Warm ``agent_id``'s sandbox via the Temporal-aware acquire dispatch.

    Imports ``agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch`` lazily so
    non-sandbox routes (registry listing, schema resolution, run history)
    never pull in ``temporalio`` at module-import time — only a call into
    this function (i.e. an actual invoke) does. Kept as a real module-level
    name, rather than an inline import at the call site, so route tests can
    still monkeypatch ``agents_route_mod.acquire`` directly.

    Preconditions:
        * ``agent_id`` is non-empty; called from a running event loop.
    Postconditions:
        * Returns the resulting sandbox handle, or raises the same
          ``UnknownAgentError`` / ``DockerUnavailableError`` types
          ``acquire_sandbox`` raises — this wrapper adds no error handling of
          its own.
    """
    from agent_team_studio.agent_provisioning_team.temporal.sandbox_dispatch import acquire_sandbox

    return await acquire_sandbox(agent_id)


@router.post("/{agent_id}/invoke")
async def invoke_agent(
    agent_id: str,
    request: Request,
    saved_input_id: str | None = Query(
        default=None,
        description="Optional saved-input id to join this run back to its source.",
    ),
) -> Response:
    """Run one agent invocation in its ephemeral sandbox and return the result.

    Flow: resolve the manifest (404 unknown; 409 if it needs live integrations),
    cap the request body (413 over cap; 400 if it carries the reserved cognition
    envelope marker), acquire the sandbox (503 Docker down / 502 warm failure /
    202 still warming), then — for a cognition agent — run the ``prepare_invoke``
    gate (idempotency claim / replay, precondition enforcement, envelope wrap)
    before proxying the POST to the sandbox.

    Once the gate has *claimed* a run, every exit obeys one of exactly three
    contracts:
        1. ``finalize_invoke`` — a parseable upstream response (any status):
           gates, persists, and completes the ledger entry.
        2. ``abandon_invoke`` — the agent provably produced nothing usable (e.g.
           an over-cap response): release the claim so a retry re-executes.
        3. hold the lease — a transport error/timeout leaves the run leased,
           because the agent may still be executing and the lease is the
           concurrent double-run guard.

    Preconditions: ``agent_id`` is a path segment; ``request`` is the live HTTP
        request. Postconditions: returns a ``Response`` (proxied result, replay,
        warming 202, or an ``HTTPException`` mapped to the status codes above);
        no claimed run is both finalized and abandoned.
    """
    # get() can hit Postgres for a dynamically-registered agent id (agent_platform.registry's
    # dynamic-manifest overlay); this is an async route, so a blocking round trip here
    # would stall the whole worker's event loop. Run it in a worker thread.
    manifest = await asyncio.to_thread(get_registry().get, agent_id)
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

    # Cap the request body *before* spinning up a sandbox — a 2 GB payload
    # must not tie up Docker or proxy memory. `read_json_capped` raises 413
    # on overflow and returns {} on empty/malformed JSON.
    body = await read_json_capped(request, max_bytes=max_payload_bytes())

    # Cognition context is injected by the platform, never accepted from the
    # untrusted caller: reject a body that already carries the reserved envelope
    # marker (DESIGN §10). Without this guard a caller could smuggle forged
    # advisory rules / memory into a cognition-enabled runtime's side channel.
    if isinstance(body, dict) and ENVELOPE_MARKER in body:
        raise HTTPException(
            status_code=400,
            detail=f"Request body must not contain the reserved key {ENVELOPE_MARKER!r}.",
        )

    try:
        handle = await acquire(agent_id)
    except DockerUnavailableError as exc:
        logger.error("Docker not available for sandbox %s: %s", agent_id, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("sandbox acquire failed for %s", agent_id)
        raise HTTPException(
            status_code=503,
            detail=f"Sandbox for agent {agent_id!r} is not available: {exc}",
        ) from exc

    if handle.status == SandboxStatus.ERROR:
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Sandbox for {agent_id} failed to warm.",
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
                "sandbox": {"agent_id": agent_id, "status": handle.status},
            },
        )

    # Pre-flight the cognition lifecycle only once the sandbox is WARM: the
    # idempotency claim (replay / 409), lazy rollup catch-up, context load, the
    # enforced precondition gate, the envelope wrap, and the full-envelope payload
    # re-cap all live in the gate. It must NOT run before the warming 202 above —
    # claiming a leased run and then telling the caller "retry shortly" would make
    # every warm-up poll conflict with its own claim until the lease expired.
    # No-op for agents without a cognition block.
    prepared = None
    post_body: Any = body
    if manifest.cognition is not None:
        outcome = await prepare_invoke(
            agent_id,
            body,
            requires_idempotency_key=manifest.cognition.requires_idempotency_key,
            idempotency_key=request.headers.get("Idempotency-Key"),
            max_envelope_bytes=max_payload_bytes(),
        )
        if outcome.kind is not GateOutcomeKind.PROCEED:
            # Every non-PROCEED outcome maps to a response through the gate's
            # single shared mapper — replay verbatim (covers blocked runs; no
            # sandbox post, no duplicate console run row), rule blocks, and the
            # reason-carrying errors. An unmapped future kind raises here
            # rather than silently proceeding ungated — a gate bypass would be
            # the worst possible default for a new failure mode.
            try:
                fin = outcome_as_finalized(outcome)
            except UnmappedGateOutcomeError as exc:
                logger.error("cognition: %s for %s", exc, agent_id)
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            headers = {"X-Khala-Replayed": "true"} if fin.replayed else None
            return JSONResponse(status_code=fin.status_code, content=fin.content, headers=headers)
        prepared = outcome.prepared
        if prepared is not None:
            post_body = prepared.envelope

    # Once the gate has claimed a run (`prepared.claim_token`), every exit from
    # this point follows one of exactly three contracts — a new early return
    # must pick one deliberately:
    #   1. finalize_invoke(...)   — a parseable upstream response (any status):
    #      gates + persistence + ledger completion.
    #   2. abandon_invoke(...)    — the agent provably produced nothing usable
    #      (e.g. the over-cap response below): release so retries re-execute.
    #   3. hold the lease         — the agent may still be executing (transport
    #      errors/timeouts): the lease is the concurrent double-run guard.
    # Outer transport timeout on the proxy → sandbox hop. The inner
    # per-agent execution timeout (enforced via asyncio.wait_for inside the
    # shim) is strictly shorter, so the shim always gets to surface a 504
    # envelope before this fires.
    timeout_s = TEAM_CONFIGS.get(manifest.team).timeout_seconds if manifest.team in TEAM_CONFIGS else 120.0
    # `post_body` is the gate's marker-wrapped envelope for cognition agents and
    # the caller body otherwise; the original `body` is preserved for run
    # persistence below (we store what the caller sent).
    target = f"{handle.url}/_agents/{agent_id}/invoke"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            if prepared is not None and prepared.envelope_bytes is not None:
                # Reuse the bytes the gate already serialized for the payload-cap
                # check instead of re-serializing the (potentially ~MiB) envelope.
                upstream = await client.post(
                    target,
                    content=prepared.envelope_bytes,
                    headers={"Content-Type": "application/json"},
                )
            else:
                upstream = await client.post(target, json=post_body)
    except httpx.HTTPError as exc:
        # The cognition claim (if any) is deliberately LEFT leased: a transport
        # error does not prove the agent isn't still executing in the sandbox,
        # and the lease is exactly what prevents a concurrent double-run. A
        # same-key retry inside the lease window gets 409 by design; after
        # expiry it re-executes.
        logger.exception("invoke proxy failed %s", target)
        raise HTTPException(status_code=502, detail=f"Sandbox invoke failed: {exc}") from exc

    # Update idle tracker only on a real response (any status — the user still engaged with it).
    await note_activity(agent_id)

    # Cap the upstream response body. A runaway sandbox that returns 10 GB
    # should not blow up the proxy or the UI — surface a 502 with a preview.
    # The sandbox envelope carries the user `output` (bounded by max_output_bytes)
    # and the cognition tool-audit (bounded by max_writeback_bytes) as independent
    # per-field caps, so the combined response budget is their sum — capping at the
    # output budget alone would 502 otherwise-valid tool-using runs whose output
    # and audit are each near their own limit.
    raw_len = len(upstream.content)
    cap = max_output_bytes() + max_writeback_bytes() + RESPONSE_ENVELOPE_OVERHEAD_BYTES
    if raw_len > cap:
        logger.warning("upstream response for %s exceeded %d bytes (got %d)", agent_id, cap, raw_len)
        # The agent ran to completion but its response is unusable — there is
        # nothing to gate, persist, or replay. Release the claim so an
        # immediate retry re-executes instead of 409-ing until lease expiry.
        if prepared is not None:
            await abandon_invoke(prepared)
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Upstream response exceeds {cap} bytes",
                "truncated": True,
                "original_size": raw_len,
                "preview": upstream.content[:cap].decode("utf-8", errors="replace"),
                "sandbox": {"agent_id": agent_id, "url": handle.url},
            },
        )

    content: Any
    try:
        content = upstream.json()
    except ValueError:
        content = {"raw": upstream.text}

    # Close out the cognition lifecycle BEFORE any persistence: the enforced
    # postcondition gate runs first, so a violated model output is never stored.
    # The gate persists the writeback / trusted tool audit and the ledger
    # envelope — which therefore never carries the per-invoke `sandbox` block
    # (a replay must not masquerade as a fresh boot).
    if prepared is not None:
        fin = await finalize_invoke(prepared, upstream.status_code, content)
        if fin.blocked:
            # The model output is dropped (console row keeps only the error for
            # audit); the gate already recorded the trusted tool audit + the
            # blocked ledger envelope so a retried violation replays this 4xx.
            _persist_run(
                agent_id=agent_id,
                team=manifest.team,
                saved_input_id=saved_input_id,
                request_body=body,
                upstream_status=fin.status_code,
                envelope={"detail": {"error": f"Blocked by cognition {fin.block_phase}: {fin.block_reason}"}},
                sandbox_url=handle.url,
                boot_ms=handle.boot_ms,
            )
            return JSONResponse(status_code=fin.status_code, content=fin.content)

    # Add the per-invoke sandbox block on a COPY: finalize stored `content` in
    # the ledger by reference, so mutating it here would leak the sandbox block
    # into the replay envelope (a replay must not masquerade as a fresh boot).
    if isinstance(content, dict) and "sandbox" not in content:
        content = {**content, "sandbox": {"agent_id": agent_id, "url": handle.url}}

    # Best-effort run persistence. Never block the invoke on storage failure.
    _persist_run(
        agent_id=agent_id,
        team=manifest.team,
        saved_input_id=saved_input_id,
        request_body=body,
        upstream_status=upstream.status_code,
        envelope=content,
        sandbox_url=handle.url,
        boot_ms=handle.boot_ms,
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
    boot_ms: int | None = None,
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
        # Surface cold-start latency in logs_tail so the runs table stays
        # schema-stable; a dedicated column can come later (#302).
        if boot_ms is not None:
            logs_tail = [f"{COLD_START_LOG_PREFIX} boot_ms={boot_ms}", *logs_tail]
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
