"""Agent Studio Stage-1 build-flow API (mounted at ``/api/agent-studio``).

Thin HTTP surface over :class:`agent_studio.service.AgentStudioService`:

    POST /api/agent-studio/conversations                       — start an authoring chat (new | refine)
    POST /api/agent-studio/conversations/{id}/messages         — send a message; assistant updates the draft
    POST /api/agent-studio/agents/from-registry/{agent_id}     — clone a registry agent into a refine draft
    POST /api/agent-studio/agents                              — save + register a finished definition

Tool discovery for the definition panel reuses the existing ``GET /api/llm-tools/``
(no new route here). Handlers are synchronous ``def`` so FastAPI runs them in its
threadpool, keeping the blocking LLM/registry calls off the event loop. Errors
map cleanly: :class:`ValueError` → 400, :class:`LookupError` → 404.

Auth: these routes carry no per-route authentication dependency, consistent with
the other team routers on the Unified API. Authentication/authorization is expected
to be enforced upstream (reverse proxy / API gateway) rather than at the application
layer for the Stage-1 backend; application-level auth is a platform-wide follow-up.
Note the ``SecurityGatewayMiddleware`` fronting all ``/api/*`` routes is an
abuse/prompt-injection scanner, **not** an authn/authz layer.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from agent_studio.models import (
    AgentDefinition,
    ConversationStateResponse,
    SaveAgentRequest,
    SaveAgentResponse,
    SendMessageRequest,
    StartConversationRequest,
)
from agent_studio.service import AgentStudioService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"])


def _build_service() -> AgentStudioService:
    """Build the process-wide service with a durable store when Postgres is on.

    With ``POSTGRES_HOST`` set the conversation store is Postgres-backed so state
    is coherent across the 4 uvicorn workers (a conversation created on one worker
    resolves on another; turns serialize via a row lock). Without it — local dev /
    tests — the in-memory store is used, exactly as before.

    The store is stateless with a lazily-opened pool, so this selection does no I/O:
    it only decides *which* store class to instantiate. The ``except`` is narrowed
    to :class:`ImportError` / :class:`ModuleNotFoundError` on purpose — the only
    non-connectivity failure possible here is a missing optional dependency (e.g.
    psycopg absent), which legitimately degrades to in-memory. A configured-but-
    unreachable Postgres is deliberately **not** downgraded: construction opens no
    connection, so a connectivity error can only surface later inside a request,
    where it propagates rather than silently forking per-worker state. Any other
    unexpected error at construction likewise propagates (fail loud) instead of
    being swallowed into a silent fallback.
    """
    try:
        from shared_postgres import is_postgres_enabled

        if is_postgres_enabled():
            from agent_studio.pg_store import PostgresAgentStudioConversationStore

            return AgentStudioService(store=PostgresAgentStudioConversationStore())
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - only a missing dep degrades
        logger.warning(
            "Postgres Agent Studio store unavailable (missing dependency); using in-memory store",
            exc_info=True,
        )
    return AgentStudioService()


# Process-wide default service. Resolved through the ``get_agent_studio_service``
# dependency below so tests inject an isolated instance via
# ``app.dependency_overrides`` rather than mutating module state — the idiomatic
# FastAPI seam.
_service = _build_service()


def get_agent_studio_service() -> AgentStudioService:
    """Dependency: the service backing the Agent Studio routes."""
    return _service


# Annotated form keeps the dependency out of the default value (ruff B008) while
# remaining overridable via ``app.dependency_overrides[get_agent_studio_service]``.
AgentStudioServiceDep = Annotated[AgentStudioService, Depends(get_agent_studio_service)]


@router.post("/conversations", response_model=ConversationStateResponse)
def start_conversation(req: StartConversationRequest, service: AgentStudioServiceDep) -> ConversationStateResponse:
    """Start an authoring conversation in ``new`` or ``refine`` mode.

    Returns the initial conversation state (greeting + seeded definition).
    Maps the service error contract to HTTP: ``ValueError`` → 400 (e.g. ``refine``
    without a source), ``LookupError`` → 404 (unknown source agent).
    """
    try:
        return service.start_conversation(req.mode, req.source_agent_id, req.initial_message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(
    conversation_id: str, req: SendMessageRequest, service: AgentStudioServiceDep
) -> ConversationStateResponse:
    """Send a user message; the assistant updates the draft and replies.

    Returns the updated conversation state. Maps the service error contract:
    ``ValueError`` → 400 (invalid input), ``LookupError`` → 404 (unknown
    conversation) — both branches present so neither escapes as a 500.
    """
    try:
        return service.send_message(conversation_id, req.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents/from-registry/{agent_id}", response_model=AgentDefinition)
def clone_from_registry(agent_id: str, service: AgentStudioServiceDep) -> AgentDefinition:
    """Clone a registered agent into an editable refine-mode draft.

    Returns the new draft definition (the source manifest is never mutated).
    ``LookupError`` → 404 when ``agent_id`` names no registered agent.
    """
    try:
        return service.clone_from_registry(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents", response_model=SaveAgentResponse)
def save_agent(req: SaveAgentRequest, service: AgentStudioServiceDep) -> SaveAgentResponse:
    """Save + register a finished definition into the live ``agent_registry``.

    Returns the registered manifest plus ``created`` (``True`` for a new agent,
    ``False`` when an existing same-id agent was updated in place). ``ValueError``
    → 400 when the definition is not ready (missing required fields).
    """
    try:
        manifest, created = service.save_agent(req.to_definition())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SaveAgentResponse(agent_id=manifest.id, manifest=manifest, created=created)
