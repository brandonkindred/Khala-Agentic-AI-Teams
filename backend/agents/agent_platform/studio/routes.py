"""Agent Studio Stage-1 build-flow API (mounted at ``/api/agent-studio``).

Conversation / agent handlers are a thin HTTP surface over Agent Studio's authoring
CRUD:

    POST /api/agent-studio/conversations                       — start an authoring chat (new | refine)
    POST /api/agent-studio/conversations/{id}/messages         — send a message; assistant updates the draft
    POST /api/agent-studio/agents/from-registry/{agent_id}     — clone a registry agent into a refine draft
    POST /api/agent-studio/agents                              — save + register a finished definition

These handlers call :mod:`agent_platform.studio.temporal.dispatch`, which
transparently picks the dispatch mode per call: a durable workflow → activity round
trip via Temporal when it's configured, or a direct in-process call otherwise — both
paths ultimately delegate to the process-wide
:class:`~agent_platform.studio.service.AgentStudioService` singleton
(:func:`agent_platform.studio.runtime.get_studio_service`), so the routes
below are unaware of which mode ran. When Temporal is enabled its worker runs
in-process (started from the unified-API lifespan), so those activity threads share
that singleton's conversation store with these handlers.

User-scoped Studio drafts are **sync store CRUD** (not Temporal) over
:func:`agent_platform.studio.drafts_runtime.get_draft_store`. Bodies are an
opaque ``{name?, payload?}`` envelope; tenancy uses :func:`get_current_user_id`
(overridable in tests via ``app.dependency_overrides``):

    POST   /api/agent-studio/drafts              — create-only
    PUT    /api/agent-studio/drafts/{draft_id}   — partial update (omitted fields unchanged)
    GET    /api/agent-studio/drafts              — list summaries
    GET    /api/agent-studio/drafts/{draft_id}   — load full draft
    PATCH  /api/agent-studio/drafts/{draft_id}   — rename
    DELETE /api/agent-studio/drafts/{draft_id}   — delete

Tool discovery for the definition panel reuses the existing ``GET /api/llm-tools/``
(no new route here). Handlers are synchronous ``def`` so FastAPI runs them in its
threadpool (Temporal round-trips and store I/O stay off the event loop). Errors map
cleanly: :class:`ValueError` → 400, :class:`LookupError` / missing draft → 404 — the
dispatch layer surfaces those same native exceptions in both dispatch modes (re-raised
from the workflow failure on the Temporal path, raised directly by the service on the
direct path), so conversation/agent mapping here is unchanged either way.

Auth: these routes carry no real authentication dependency, consistent with the
other team routers on the Unified API. Authentication/authorization is expected to be
enforced upstream (reverse proxy / API gateway). Drafts tenancy is currently a
pluggable user-id dependency that defaults to ``DEFAULT_USER_ID`` until real auth is
wired. The ``SecurityGatewayMiddleware`` fronting all ``/api/*`` routes is an
abuse/prompt-injection scanner, **not** an authn/authz layer.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_platform.studio.drafts_runtime import get_draft_store
from agent_platform.studio.models import (
    AgentDefinition,
    AgentStudioDraft,
    AgentStudioDraftSummary,
    ConversationStateResponse,
    RenameDraftRequest,
    SaveAgentRequest,
    SaveAgentResponse,
    SaveDraftRequest,
    SendMessageRequest,
    StartConversationRequest,
)
from agent_platform.studio.temporal import dispatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"])


def get_current_user_id() -> str:
    """Resolve the caller user id for drafts tenancy.

    Postconditions:
        * Returns a non-empty user id. Default is ``DEFAULT_USER_ID`` until real
          auth is wired; tests override via ``app.dependency_overrides``.

    Note:
        ``DEFAULT_USER_ID`` is imported lazily so ``user_profile`` stays out of
        ``sys.modules`` when that team is disabled at import time.
    """
    from user_profile.store import DEFAULT_USER_ID

    return DEFAULT_USER_ID


def _summary_from_draft(draft: AgentStudioDraft) -> AgentStudioDraftSummary:
    return AgentStudioDraftSummary(
        draft_id=draft.draft_id, name=draft.name, updated_at=draft.updated_at
    )


@router.post("/drafts", response_model=AgentStudioDraftSummary)
def create_draft(
    req: SaveDraftRequest,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraftSummary:
    """Create a new Studio draft owned by the current user.

    Preconditions:
        * ``req`` is FastAPI-validated; ``user_id`` is non-empty from the dependency.
    Postconditions:
        * Returns a summary for the new draft. ``ValueError`` → 400.
    """
    try:
        draft = get_draft_store().create(user_id, name=req.name, payload=req.payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _summary_from_draft(draft)


@router.put("/drafts/{draft_id}", response_model=AgentStudioDraftSummary)
def update_draft(
    draft_id: str,
    req: SaveDraftRequest,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraftSummary:
    """Partially update an owned draft; omitted ``name``/``payload`` stay unchanged.

    Postconditions:
        * Returns updated summary, or 404 when missing/wrong user. ``ValueError`` → 400.
        * Fields left ``None`` in ``req`` are not cleared — send ``payload: {}`` to
          replace the payload with an empty object.
    """
    try:
        draft = get_draft_store().update(user_id, draft_id, name=req.name, payload=req.payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return _summary_from_draft(draft)


@router.get("/drafts", response_model=list[AgentStudioDraftSummary])
def list_drafts(
    user_id: Annotated[str, Depends(get_current_user_id)],
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
) -> list[AgentStudioDraftSummary]:
    """List draft summaries for the current user (most recent first).

    Postconditions:
        * Returns summaries only; store clamps ``limit`` to max 100.
    """
    return get_draft_store().list_summaries(user_id, limit=limit, offset=offset)


@router.get("/drafts/{draft_id}", response_model=AgentStudioDraft)
def get_draft(
    draft_id: str,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraft:
    """Load the full draft payload for the current user."""
    draft = get_draft_store().get(user_id, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@router.patch("/drafts/{draft_id}", response_model=AgentStudioDraftSummary)
def rename_draft(
    draft_id: str,
    req: RenameDraftRequest,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraftSummary:
    """Rename an owned draft."""
    try:
        summary = get_draft_store().rename(user_id, draft_id, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if summary is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return summary


@router.delete("/drafts/{draft_id}")
def delete_draft(
    draft_id: str,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict[str, str]:
    """Delete an owned draft."""
    if not get_draft_store().delete(user_id, draft_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"draft_id": draft_id, "status": "deleted"}


@router.post("/conversations", response_model=ConversationStateResponse)
def start_conversation(req: StartConversationRequest) -> ConversationStateResponse:
    """Start an authoring conversation in ``new`` or ``refine`` mode.

    Preconditions:
        - ``req`` is a ``StartConversationRequest`` already validated by FastAPI (422
          otherwise, before this body runs).

    Postconditions:
        - Returns the initial ``ConversationStateResponse`` (greeting + seeded
          definition). Maps the service error contract to HTTP: ``ValueError`` → 400
          (e.g. ``refine`` without a source), ``LookupError`` → 404 (unknown source
          agent); no other exception is translated here.
    """
    try:
        return dispatch.start_conversation(req.mode, req.source_agent_id, req.initial_message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(conversation_id: str, req: SendMessageRequest) -> ConversationStateResponse:
    """Send a user message; the assistant updates the draft and replies.

    Preconditions:
        - ``conversation_id`` is a non-empty path segment; ``req`` is a validated
          ``SendMessageRequest`` (FastAPI returns 422 otherwise before this body runs).

    Postconditions:
        - Returns the updated ``ConversationStateResponse``. Maps the service error
          contract: ``ValueError`` → 400 (invalid input), ``LookupError`` → 404
          (unknown conversation) — both branches present so neither escapes as a 500.
    """
    try:
        return dispatch.send_message(conversation_id, req.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents/from-registry/{agent_id}", response_model=AgentDefinition)
def clone_from_registry(agent_id: str) -> AgentDefinition:
    """Clone a registered agent into an editable refine-mode draft.

    Preconditions:
        - ``agent_id`` is a non-empty path segment naming a candidate registry agent.

    Postconditions:
        - Returns the new draft ``AgentDefinition`` (the source manifest is never
          mutated). ``LookupError`` → 404 when ``agent_id`` names no registered agent.
    """
    try:
        return dispatch.clone_from_registry(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents", response_model=SaveAgentResponse)
def save_agent(req: SaveAgentRequest) -> SaveAgentResponse:
    """Save + register a finished definition into the live ``agent_registry``.

    Preconditions:
        - ``req`` is a validated ``SaveAgentRequest`` (FastAPI returns 422 otherwise
          before this body runs); ``req.to_definition()`` yields the definition to save.

    Postconditions:
        - Returns a ``SaveAgentResponse`` carrying the registered manifest plus
          ``created`` (``True`` for a new agent, ``False`` when an existing same-id
          agent was updated in place). ``ValueError`` → 400 when the definition is not
          ready (missing required fields).
    """
    try:
        manifest, created = dispatch.save_agent(req.to_definition())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SaveAgentResponse(agent_id=manifest.id, manifest=manifest, created=created)
