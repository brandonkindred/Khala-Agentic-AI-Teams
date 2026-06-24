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

from fastapi import APIRouter, HTTPException

from agent_studio.models import (
    AgentDefinition,
    ConversationStateResponse,
    SaveAgentResponse,
    SendMessageRequest,
    StartConversationRequest,
)
from agent_studio.service import AgentStudioService

router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"])

# Process-wide service (in-memory conversation store). Tests replace this with an
# isolated instance via ``agent_studio_routes._service = ...``.
_service = AgentStudioService()


@router.post("/conversations", response_model=ConversationStateResponse)
def start_conversation(req: StartConversationRequest) -> ConversationStateResponse:
    try:
        return _service.start_conversation(req.mode, req.source_agent_id, req.initial_message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(conversation_id: str, req: SendMessageRequest) -> ConversationStateResponse:
    try:
        return _service.send_message(conversation_id, req.message)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents/from-registry/{agent_id}", response_model=AgentDefinition)
def clone_from_registry(agent_id: str) -> AgentDefinition:
    try:
        return _service.clone_from_registry(agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agents", response_model=SaveAgentResponse)
def save_agent(definition: AgentDefinition) -> SaveAgentResponse:
    try:
        manifest = _service.save_agent(definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SaveAgentResponse(agent_id=manifest.id, manifest=manifest)
