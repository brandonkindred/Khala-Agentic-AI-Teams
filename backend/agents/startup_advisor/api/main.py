"""FastAPI endpoints for the Startup Advisor persistent chat."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared_observability import init_otel, instrument_fastapi_app
from startup_advisor.postgres import SCHEMA as STARTUP_ADVISOR_POSTGRES_SCHEMA
from startup_advisor.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    cancel_job,
    create_job,
    delete_job,
    get_job,
    is_job_cancelled,
    list_jobs,
    update_job,
)

logger = logging.getLogger(__name__)

init_otel(service_name="startup-advisor", team_key="startup_advisor")


@asynccontextmanager
async def _lifespan(application: FastAPI):
    try:
        from shared_postgres import register_team_schemas

        register_team_schemas(STARTUP_ADVISOR_POSTGRES_SCHEMA)
    except Exception:
        logger.exception("startup_advisor postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("startup_advisor shared_postgres close_pool failed", exc_info=True)


app = FastAPI(
    title="Startup Advisor API",
    description="Persistent conversational startup advisor with probing dialogue",
    version="1.0.0",
    lifespan=_lifespan,
)
instrument_fastapi_app(app, team_key="startup_advisor")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    initial_message: Optional[str] = Field(
        default=None, description="Optional first message from the founder"
    )


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

    return _build_response(
        cid, messages, context, artifacts, _DEFAULT_SUGGESTED if len(messages) <= 1 else []
    )


class SendMessageJobResponse(BaseModel):
    job_id: str
    status: str = JOB_STATUS_PENDING


class SendMessageJobStatus(BaseModel):
    job_id: str
    status: str
    result: Optional[ConversationStateResponse] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SendMessageJobListItem(BaseModel):
    job_id: str
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SendMessageJobListResponse(BaseModel):
    jobs: List[SendMessageJobListItem]


def _run_advisor_message_background(job_id: str, message: str) -> None:
    try:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = _process_advisor_message(message)
        if is_job_cancelled(job_id):
            return
        update_job(
            job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump(mode="json")
        )
    except Exception as exc:
        logger.exception("Startup advisor job %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))


def _process_advisor_message(message: str) -> ConversationStateResponse:
    store = _get_store()
    agent = _get_agent()

    cid = store.get_or_create_singleton()
    state = store.get(cid)
    if state is None:
        raise RuntimeError("Failed to load conversation")
    messages, context = state

    if len(messages) == 0:
        store.append_message(cid, "assistant", _WELCOME_MESSAGE)
        state = store.get(cid)
        if state is None:
            raise RuntimeError("Failed to load conversation")
        messages, context = state

    store.append_message(cid, "user", message)

    msg_pairs = [(m.role, m.content) for m in messages]
    msg_pairs.append(("user", message))

    reply, context_update, suggested_questions, artifact = agent.respond(
        msg_pairs, context, message
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
        raise RuntimeError("Failed to reload conversation")
    messages, context = state
    artifacts = store.get_artifacts(cid)
    return _build_response(cid, messages, context, artifacts, suggested_questions)


@app.post("/conversation/messages", response_model=SendMessageJobResponse)
def send_message(payload: SendMessageRequest) -> SendMessageJobResponse:
    """Submit a message to the startup advisor. Poll
    ``GET /conversation/messages/status/{job_id}`` for the updated
    ``ConversationStateResponse`` in the ``result`` field.
    """
    job_id = str(uuid4())
    create_job(job_id, message=payload.message)
    thread = threading.Thread(
        target=_run_advisor_message_background,
        args=(job_id, payload.message),
        daemon=True,
    )
    thread.start()
    return SendMessageJobResponse(job_id=job_id, status=JOB_STATUS_PENDING)


@app.get("/conversation/messages/status/{job_id}", response_model=SendMessageJobStatus)
def get_advisor_job_status(job_id: str) -> SendMessageJobStatus:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return SendMessageJobStatus(
        job_id=data.get("job_id", job_id),
        status=data.get("status", JOB_STATUS_PENDING),
        result=data.get("result"),
        error=data.get("error"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@app.get("/conversation/messages/jobs", response_model=SendMessageJobListResponse)
def list_advisor_jobs(running_only: bool = False) -> SendMessageJobListResponse:
    statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    items = [
        SendMessageJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            created_at=j.get("created_at"),
            updated_at=j.get("updated_at"),
        )
        for j in list_jobs(statuses=statuses)
    ]
    return SendMessageJobListResponse(jobs=items)


@app.post("/conversation/messages/jobs/{job_id}/cancel")
def cancel_advisor_job(job_id: str) -> Dict[str, Any]:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if cancel_job(job_id):
        return {"job_id": job_id, "status": JOB_STATUS_CANCELLED, "success": True}
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "success": False,
        "message": f"Cannot cancel job in status {data.get('status')}",
    }


@app.delete("/conversation/messages/jobs/{job_id}")
def delete_advisor_job(job_id: str) -> Dict[str, Any]:
    if get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "deleted": True}


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
