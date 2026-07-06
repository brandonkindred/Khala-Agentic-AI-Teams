"""FastAPI endpoints for the Startup Advisor persistent chat."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from shared_app import create_team_app
from startup_advisor.pipeline import (
    DEFAULT_SUGGESTED,
    WELCOME_MESSAGE,
    ArtifactResponse,
    ConversationStateResponse,
    build_response,
    get_store,
    run_advisor_core,
)
from startup_advisor.postgres import SCHEMA as STARTUP_ADVISOR_POSTGRES_SCHEMA
from startup_advisor.shared.job_store import (
    JOB_STATUS_CANCELLED,
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


def _startup() -> None:
    """Start the Temporal worker backstop (best-effort).

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` before uvicorn accepts requests; this
    backstop covers running the app standalone (``uvicorn ...:app``).

    Preconditions:
        - None (safe to call once at app startup).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — any failure is logged
          as a warning so it cannot abort app boot (this runs as an
          ``on_startup`` hook).
    """
    try:
        from startup_advisor.temporal.worker import (
            start_startup_advisor_temporal_worker_thread,
        )

        start_startup_advisor_temporal_worker_thread()
    except Exception:
        logger.warning(
            "startup_advisor Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )


app = create_team_app(
    service_name="startup-advisor",
    team_key="startup_advisor",
    title="Startup Advisor API",
    description="Persistent conversational startup advisor with probing dialogue",
    version="1.0.0",
    postgres_schema=STARTUP_ADVISOR_POSTGRES_SCHEMA,
    on_startup=_startup,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    initial_message: Optional[str] = Field(
        default=None, description="Optional first message from the founder"
    )


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    created_at: str
    updated_at: str
    message_count: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/conversation", response_model=ConversationStateResponse)
def get_or_create_conversation() -> ConversationStateResponse:
    """Get the singleton conversation, creating it with a welcome message if it doesn't exist."""
    store = get_store()
    cid = store.get_or_create_singleton()
    state = store.get(cid)
    if state is None:
        raise HTTPException(status_code=500, detail="Failed to load conversation")

    messages, context = state
    artifacts = store.get_artifacts(cid)

    # If the conversation is brand new (no messages), add the welcome message
    if len(messages) == 0:
        store.append_message(cid, "assistant", WELCOME_MESSAGE)
        state = store.get(cid)
        if state is None:
            raise HTTPException(status_code=500, detail="Failed to load conversation")
        messages, context = state

    return build_response(
        cid, messages, context, artifacts, DEFAULT_SUGGESTED if len(messages) <= 1 else []
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
    """Thread-path runner: execute the advisor core and swallow failures as FAILED.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - Delegates to ``run_advisor_core`` for the RUNNING/COMPLETED
          transition.
        - On any exception from ``run_advisor_core``, logs it and marks the
          job FAILED (unless the job was cancelled in the meantime), instead
          of letting the exception escape the thread.
        - If marking the job FAILED itself raises (e.g. the job store is
          unreachable), that failure is logged and swallowed too — this is
          the thread's top-level function, so nothing propagates and the
          thread cannot die with an unhandled exception.
    """
    try:
        run_advisor_core(job_id, message)
    except Exception as exc:
        logger.exception("Startup advisor job %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        try:
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))
        except Exception:
            logger.exception("Failed to mark startup advisor job %s as FAILED", job_id)


def _dispatch_advisor_message(job_id: str, message: str) -> str:
    """Dispatch an advisor message via Temporal when enabled, else a daemon thread.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - Starts exactly one execution path and returns its label
          ("Temporal" or "thread"). With ``TEMPORAL_ADDRESS`` set the run is
          started as a durable ``StartupAdvisorWorkflow``; otherwise the
          legacy thread path runs unchanged.
        - A missing ``shared_temporal`` (Temporal not installed) falls
          through to the thread path; any *other* failure while starting the
          workflow propagates to the caller, which marks the job FAILED — a
          Temporal-enabled run is never silently downgraded.
    """
    try:
        from shared_temporal import is_temporal_enabled
    except ImportError:
        is_temporal_enabled = None

    if is_temporal_enabled is not None and is_temporal_enabled():
        from startup_advisor.temporal.start_workflow import start_startup_advisor_workflow

        start_startup_advisor_workflow(job_id, message)
        logger.info("Startup advisor job dispatched via Temporal: job_id=%s", job_id)
        return "Temporal"

    thread = threading.Thread(
        target=_run_advisor_message_background,
        args=(job_id, message),
        daemon=True,
    )
    thread.start()
    return "thread"


@app.post("/conversation/messages", response_model=SendMessageJobResponse)
def send_message(payload: SendMessageRequest) -> SendMessageJobResponse:
    """Submit a message to the startup advisor. Poll
    ``GET /conversation/messages/status/{job_id}`` for the updated
    ``ConversationStateResponse`` in the ``result`` field.
    """
    job_id = str(uuid4())
    create_job(job_id, message=payload.message)

    try:
        _dispatch_advisor_message(job_id, payload.message)
    except Exception as exc:
        # A dispatch failure (e.g. the Temporal worker client never connected)
        # must not leave the freshly-created job orphaned in PENDING — mark it
        # FAILED so callers polling status see a terminal state.
        logger.exception("Failed to dispatch startup advisor job %s", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=f"Dispatch failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to start startup advisor run.") from exc

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
    store = get_store()
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
