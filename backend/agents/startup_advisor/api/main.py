"""FastAPI endpoints for the Startup Advisor persistent chat."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Startup Advisor API",
    description="Persistent conversational startup advisor with probing dialogue",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    initial_message: Optional[str] = Field(default=None, description="Optional first message from the founder")


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    timestamp: str


class ArtifactResponse(BaseModel):
    artifact_id: int
    artifact_type: str
    title: str
    payload: dict[str, Any]
    created_at: str


class ConversationStateResponse(BaseModel):
    conversation_id: str
    messages: list[ConversationMessageResponse]
    context: dict[str, Any]
    artifacts: list[ArtifactResponse]
    suggested_questions: list[str]


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    created_at: str
    updated_at: str
    message_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WELCOME_MESSAGE = (
    "Welcome! I'm your startup advisor. I'm here to help you think through "
    "your startup strategy — from customer discovery to fundraising to execution.\n\n"
    "To give you the best advice, I'll need to understand your situation first. "
    "Let's start: what are you working on, and what stage is your startup at?"
)

_DEFAULT_SUGGESTED = [
    "I'm validating a new idea and need help with customer discovery.",
    "I'm building an MVP and want to prioritize features.",
    "I need help with my go-to-market strategy.",
]


def _get_store():  # noqa: ANN202
    from startup_advisor.store import get_conversation_store

    return get_conversation_store()


def _get_agent():  # noqa: ANN202
    from startup_advisor.assistant.agent import get_advisor_agent

    return get_advisor_agent()


def _build_response(
    conversation_id: str,
    messages,  # noqa: ANN001
    context: dict[str, Any],
    artifacts,  # noqa: ANN001
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


def _merge_context(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge context_update into existing context, preserving prior values."""
    merged = dict(existing)
    for key, value in update.items():
        if value is not None and value != "":
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/conversation", response_model=ConversationStateResponse)
def get_or_create_conversation() -> ConversationStateResponse:
    """Get the singleton conversation, creating it with a welcome message if it doesn't exist."""
    store = _get_store()
    cid = store.get_or_create_singleton()
    state = store.get(cid)
    if state is None:
        raise HTTPException(status_code=500, detail="Failed to load conversation")

    messages, context = state
    artifacts = store.get_artifacts(cid)

    # If the conversation is brand new (no messages), add the welcome message
    if len(messages) == 0:
        store.append_message(cid, "assistant", _WELCOME_MESSAGE)
        state = store.get(cid)
        if state is None:
            raise HTTPException(status_code=500, detail="Failed to load conversation")
        messages, context = state

    return _build_response(cid, messages, context, artifacts, _DEFAULT_SUGGESTED if len(messages) <= 1 else [])


@app.post("/conversation/messages", response_model=ConversationStateResponse)
def send_message(payload: SendMessageRequest) -> ConversationStateResponse:
    """Send a message to the startup advisor and get a response."""
    store = _get_store()
    agent = _get_agent()

    cid = store.get_or_create_singleton()
    state = store.get(cid)
    if state is None:
        raise HTTPException(status_code=500, detail="Failed to load conversation")

    messages, context = state

    # If no messages yet, add welcome first
    if len(messages) == 0:
        store.append_message(cid, "assistant", _WELCOME_MESSAGE)
        state = store.get(cid)
        if state is None:
            raise HTTPException(status_code=500, detail="Failed to load conversation")
        messages, context = state

    # Persist user message
    store.append_message(cid, "user", payload.message)

    # Build history for LLM
    msg_pairs = [(m.role, m.content) for m in messages]
    msg_pairs.append(("user", payload.message))

    # Get advisor response
    reply, context_update, suggested_questions, artifact = agent.respond(
        msg_pairs, context, payload.message
    )

    # Merge and persist context updates
    if context_update:
        context = _merge_context(context, context_update)
        store.update_context(cid, context)

    # Persist assistant reply
    store.append_message(cid, "assistant", reply)

    # Persist artifact if produced
    if artifact and isinstance(artifact, dict):
        artifact_type = artifact.get("type", "advice")
        title = artifact.get("title", "Untitled")
        content = artifact.get("content", artifact)
        store.add_artifact(cid, artifact_type, title, content)

    # Reload full state
    state = store.get(cid)
    if state is None:
        raise HTTPException(status_code=500, detail="Failed to reload conversation")
    messages, context = state
    artifacts = store.get_artifacts(cid)

    return _build_response(cid, messages, context, artifacts, suggested_questions)


@app.get("/conversation/artifacts", response_model=list[ArtifactResponse])
def list_artifacts() -> list[ArtifactResponse]:
    """List all artifacts produced during the conversation."""
    store = _get_store()
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
