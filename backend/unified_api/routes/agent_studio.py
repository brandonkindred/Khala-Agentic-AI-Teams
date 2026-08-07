"""Agent Studio Stage-1 build-flow API (mounted at ``/api/agent-studio``).

Thin HTTP surface over the Agent Studio Temporal workflows:

    POST /api/agent-studio/conversations                       — start an authoring chat (new | refine)
    POST /api/agent-studio/conversations/{id}/messages         — send a message; assistant updates the draft
    POST /api/agent-studio/agents/from-registry/{agent_id}     — clone a registry agent into a refine draft
    POST /api/agent-studio/agents                              — save + register a finished definition

Agent Studio is **Temporal-only**: every handler dispatches its operation as a
durable workflow → activity via :mod:`agent_team_studio.agent_studio.temporal.dispatch` and blocks for
the result. There is no non-Temporal fallback — the activity does the real work by
delegating to the process-wide :class:`~agent_team_studio.agent_studio.service.AgentStudioService`
singleton (:func:`agent_team_studio.agent_studio.runtime.get_studio_service`). The worker runs
in-process (started from the unified-API lifespan), so those activity threads share
that singleton's conversation store with these handlers.

Tool discovery for the definition panel reuses the existing ``GET /api/llm-tools/``
(no new route here). Handlers are synchronous ``def`` so FastAPI runs them in its
threadpool, keeping the blocking workflow round-trip off the event loop. Errors map
cleanly: :class:`ValueError` → 400, :class:`LookupError` → 404 — the dispatch layer
re-raises those native exceptions from the workflow failure so this mapping is
unchanged by the Temporal round-trip.

Auth: these routes carry no per-route authentication dependency, consistent with the
other team routers on the Unified API. Authentication/authorization is expected to be
enforced upstream (reverse proxy / API gateway). The ``SecurityGatewayMiddleware``
fronting all ``/api/*`` routes is an abuse/prompt-injection scanner, **not** an
authn/authz layer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agent_team_studio.agent_studio.models import (
    AgentDefinition,
    ConversationStateResponse,
    SaveAgentRequest,
    SaveAgentResponse,
    SendMessageRequest,
    StartConversationRequest,
)
from agent_team_studio.agent_studio.temporal import dispatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"])


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
