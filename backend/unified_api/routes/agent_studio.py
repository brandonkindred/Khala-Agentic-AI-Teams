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
"""

from __future__ import annotations

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

router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"])

# Process-wide default service (in-memory conversation store). It is resolved
# through the ``get_agent_studio_service`` dependency below so tests inject an
# isolated instance via ``app.dependency_overrides`` rather than mutating module
# state — the idiomatic FastAPI seam.
_service = AgentStudioService()


def get_agent_studio_service() -> AgentStudioService:
    """Dependency: the service backing the Agent Studio routes."""
    return _service


# Annotated form keeps the dependency out of the default value (ruff B008) while
# remaining overridable via ``app.dependency_overrides[get_agent_studio_service]``.
ServiceDep = Annotated[AgentStudioService, Depends(get_agent_studio_service)]


@router.post("/conversations", response_model=ConversationStateResponse)
def start_conversation(req: StartConversationRequest, service: ServiceDep) -> ConversationStateResponse:
    try:
        return service.start_conversation(req.mode, req.source_agent_id, req.initial_message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(conversation_id: str, req: SendMessageRequest, service: ServiceDep) -> ConversationStateResponse:
    try:
        return service.send_message(conversation_id, req.message)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents/from-registry/{agent_id}", response_model=AgentDefinition)
def clone_from_registry(agent_id: str, service: ServiceDep) -> AgentDefinition:
    try:
        return service.clone_from_registry(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents", response_model=SaveAgentResponse)
def save_agent(req: SaveAgentRequest, service: ServiceDep) -> SaveAgentResponse:
    try:
        manifest = service.save_agent(req.to_definition())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SaveAgentResponse(agent_id=manifest.id, manifest=manifest)
