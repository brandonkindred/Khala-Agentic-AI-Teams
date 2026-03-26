"""Factory that creates a FastAPI sub-app for a team assistant.

Each team gets its own sub-app mounted at ``{team_prefix}/assistant``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from team_assistant.agent import TeamAssistantAgent
from team_assistant.config import TeamAssistantConfig
from team_assistant.prompts import build_system_prompt
from team_assistant.store import get_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared request / response models
# ---------------------------------------------------------------------------


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class UpdateContextRequest(BaseModel):
    context: Dict[str, Any]


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    timestamp: str


class ArtifactResponse(BaseModel):
    artifact_id: int
    artifact_type: str
    title: str
    payload: Dict[str, Any]
    created_at: str


class ConversationStateResponse(BaseModel):
    conversation_id: str
    messages: List[ConversationMessageResponse]
    context: Dict[str, Any]
    artifacts: List[ArtifactResponse]
    suggested_questions: List[str]


class ReadinessResponse(BaseModel):
    ready: bool
    missing_fields: List[str]
    context: Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merge_context(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in update.items():
        if value is not None and value != "":
            merged[key] = value
    return merged


def _build_state_response(
    conversation_id: str,
    messages: list,
    context: dict[str, Any],
    artifacts: list,
    suggested_questions: list[str],
) -> ConversationStateResponse:
    return ConversationStateResponse(
        conversation_id=conversation_id,
        messages=[
            ConversationMessageResponse(role=m.role, content=m.content, timestamp=m.timestamp)
            for m in messages
        ],
        context=context,
        artifacts=[
            ArtifactResponse(
                artifact_id=a.artifact_id,
                artifact_type=a.artifact_type,
                title=a.title,
                payload=a.payload,
                created_at=a.created_at,
            )
            for a in artifacts
        ],
        suggested_questions=suggested_questions,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Cache agents per team_key so we don't rebuild on every request
_agents: dict[str, TeamAssistantAgent] = {}


def _get_agent(config: TeamAssistantConfig) -> TeamAssistantAgent:
    if config.team_key not in _agents:
        _agents[config.team_key] = TeamAssistantAgent(
            team_name=config.team_name,
            system_prompt=build_system_prompt(config),
            welcome_message=config.welcome_message,
            default_suggested_questions=config.default_suggested_questions,
            required_fields=config.required_fields,
            llm_agent_key=config.llm_agent_key,
        )
    return _agents[config.team_key]


def create_assistant_app(config: TeamAssistantConfig) -> FastAPI:
    """Return a FastAPI sub-app with conversation endpoints for the given team."""

    assistant_app = FastAPI(
        title=f"{config.team_name} Assistant API",
        description=f"Conversational assistant for the {config.team_name} team",
        version="1.0.0",
    )

    # Freeze config in closure
    team_key = config.team_key
    welcome_message = config.welcome_message
    default_suggested: list[str] = list(config.default_suggested_questions)

    @assistant_app.get("/conversation", response_model=ConversationStateResponse)
    def get_or_create_conversation() -> ConversationStateResponse:
        """Get the singleton conversation, creating it with a welcome message if none exists."""
        store = get_store(team_key)
        cid = store.get_or_create_singleton()
        state = store.get(cid)
        if state is None:
            raise HTTPException(status_code=500, detail="Failed to load conversation")

        messages, context = state
        artifacts = store.get_artifacts(cid)

        if len(messages) == 0:
            store.append_message(cid, "assistant", welcome_message)
            state = store.get(cid)
            if state is None:
                raise HTTPException(status_code=500, detail="Failed to load conversation")
            messages, context = state

        return _build_state_response(
            cid, messages, context, artifacts, default_suggested if len(messages) <= 1 else []
        )

    @assistant_app.post("/conversation/messages", response_model=ConversationStateResponse)
    def send_message(payload: SendMessageRequest) -> ConversationStateResponse:
        """Send a message to the assistant and get a response."""
        store = get_store(team_key)
        agent = _get_agent(config)

        cid = store.get_or_create_singleton()
        state = store.get(cid)
        if state is None:
            raise HTTPException(status_code=500, detail="Failed to load conversation")

        messages, context = state

        if len(messages) == 0:
            store.append_message(cid, "assistant", welcome_message)
            state = store.get(cid)
            if state is None:
                raise HTTPException(status_code=500, detail="Failed to load conversation")
            messages, context = state

        store.append_message(cid, "user", payload.message)

        msg_pairs = [(m.role, m.content) for m in messages]
        msg_pairs.append(("user", payload.message))

        reply, context_update, suggested_questions, artifact = agent.respond(
            msg_pairs, context, payload.message
        )

        if context_update:
            context = _merge_context(context, context_update)
            store.update_context(cid, context)

        store.append_message(cid, "assistant", reply)

        if artifact and isinstance(artifact, dict):
            artifact_type = artifact.get("type", "advice")
            title = artifact.get("title", "Untitled")
            content = artifact.get("content", artifact)
            store.add_artifact(cid, artifact_type, title, content)

        state = store.get(cid)
        if state is None:
            raise HTTPException(status_code=500, detail="Failed to reload conversation")
        messages, context = state
        artifacts = store.get_artifacts(cid)

        return _build_state_response(cid, messages, context, artifacts, suggested_questions)

    @assistant_app.put("/conversation/context", response_model=ConversationStateResponse)
    def update_context(payload: UpdateContextRequest) -> ConversationStateResponse:
        """Manually update context fields (e.g. from form auto-fill)."""
        store = get_store(team_key)
        cid = store.get_or_create_singleton()
        state = store.get(cid)
        if state is None:
            raise HTTPException(status_code=500, detail="Failed to load conversation")

        messages, context = state
        context = _merge_context(context, payload.context)
        store.update_context(cid, context)

        artifacts = store.get_artifacts(cid)
        return _build_state_response(cid, messages, context, artifacts, [])

    @assistant_app.get("/conversation/artifacts", response_model=list[ArtifactResponse])
    def list_artifacts() -> list[ArtifactResponse]:
        """List all artifacts produced during the conversation."""
        store = get_store(team_key)
        cid = store.get_or_create_singleton()
        artifacts = store.get_artifacts(cid)
        return [
            ArtifactResponse(
                artifact_id=a.artifact_id,
                artifact_type=a.artifact_type,
                title=a.title,
                payload=a.payload,
                created_at=a.created_at,
            )
            for a in artifacts
        ]

    @assistant_app.get("/readiness", response_model=ReadinessResponse)
    def check_readiness() -> ReadinessResponse:
        """Check if all required fields are present in context."""
        store = get_store(team_key)
        agent = _get_agent(config)
        cid = store.get_or_create_singleton()
        state = store.get(cid)
        if state is None:
            return ReadinessResponse(ready=False, missing_fields=[], context={})
        _, context = state
        ready, missing = agent.check_readiness(context)
        return ReadinessResponse(ready=ready, missing_fields=missing, context=context)

    @assistant_app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return assistant_app
