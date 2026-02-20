"""
FastAPI application for the software engineering team.

Async API: POST /run-team returns job_id, GET /run-team/{job_id} polls status.
Tech Lead orchestrator runs in background.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Path setup for imports when run as uvicorn from project root
import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from spec_parser import validate_work_path
from shared.clarification_store import clarification_store
from shared.execution_tracker import execution_tracker
from shared.job_store import JOB_STATUS_FAILED, JOB_STATUS_PENDING, create_job, get_job, update_job

from shared.logging_config import setup_logging

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Software Engineering Team API",
    description="Async API: POST /run-team with work folder path returns job_id. "
    "GET /run-team/{job_id} polls status. Tech Lead orchestrates the full pipeline.",
    version="0.3.0",
)


class RunTeamRequest(BaseModel):
    """Request body for the run-team endpoint."""

    repo_path: str = Field(
        ...,
        description="Local filesystem path to the folder where work will be saved. Must contain initial_spec.md at the root. Does not need to be a git repository.",
    )


class RunTeamResponse(BaseModel):
    """Response from POST /run-team."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running", description="Initial status.")
    message: str = Field(default="Orchestrator started. Poll GET /run-team/{job_id} for status.")


class FailedTaskDetail(BaseModel):
    """Detail about a single failed task."""

    task_id: str = Field(..., description="ID of the failed task.")
    title: str = Field(default="", description="Task title.")
    reason: str = Field(default="", description="Why the task failed.")


class JobStatusResponse(BaseModel):
    """Response from GET /run-team/{job_id}."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(
        ...,
        description="pending, running, completed, failed, or paused_llm_limit (Ollama weekly usage limit exceeded; call retry-failed after limit resets).",
    )
    repo_path: Optional[str] = Field(None, description="Path to the repo.")
    requirements_title: Optional[str] = Field(None, description="Parsed project title.")
    architecture_overview: Optional[str] = Field(None, description="Architecture overview.")
    current_task: Optional[str] = Field(None, description="Current task being executed.")
    task_results: list = Field(default_factory=list, description="Completed task results.")
    task_ids: list = Field(default_factory=list, description="Task IDs in execution order.")
    progress: Optional[int] = Field(None, description="Progress percentage.")
    error: Optional[str] = Field(None, description="Error message if failed.")
    failed_tasks: List[FailedTaskDetail] = Field(
        default_factory=list,
        description="Details about tasks that failed, including the reason for failure.",
    )


class RetryResponse(BaseModel):
    """Response from POST /run-team/{job_id}/retry-failed."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(default="running", description="Status after retry start.")
    retrying_tasks: List[str] = Field(default_factory=list, description="Task IDs being retried.")
    message: str = Field(default="")


class ClarificationCreateRequest(BaseModel):
    spec_text: str = Field(..., description="Initial product/engineering specification text.")


class ClarificationMessageRequest(BaseModel):
    message: str = Field(..., description="User clarification response message.")


class ClarificationResponse(BaseModel):
    session_id: str
    assistant_message: str
    open_questions: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    done_clarifying: bool = False
    refined_spec: Optional[str] = None


class ClarificationSessionResponse(BaseModel):
    session_id: str
    spec_text: str
    status: str
    created_at: str
    clarification_round: int
    max_rounds: int
    confidence_score: float
    open_questions: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    refined_spec: Optional[str] = None
    turns: List[Dict[str, str]] = Field(default_factory=list)


def _run_orchestrator_background(job_id: str, repo_path: str) -> None:
    """Run orchestrator in background thread."""
    try:
        from orchestrator import run_orchestrator
        run_orchestrator(job_id, repo_path)
    except Exception as e:
        logger.exception("Orchestrator failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


def _run_retry_background(job_id: str) -> None:
    """Run retry in background thread."""
    try:
        from orchestrator import run_failed_tasks
        run_failed_tasks(job_id)
    except Exception as e:
        logger.exception("Retry orchestrator failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


@app.post(
    "/run-team",
    response_model=RunTeamResponse,
    summary="Start software engineering team",
    description="Validates work folder, creates job, starts Tech Lead orchestrator in background. "
    "Returns job_id immediately. Poll GET /run-team/{job_id} for status.",
)
def run_team(request: RunTeamRequest) -> RunTeamResponse:
    """Start the software engineering team on a work folder."""
    try:
        repo_path = validate_work_path(request.repo_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    job_id = str(uuid.uuid4())
    create_job(job_id, str(repo_path))

    thread = threading.Thread(target=_run_orchestrator_background, args=(job_id, str(repo_path)))
    thread.daemon = True
    thread.start()

    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Orchestrator started. Poll GET /run-team/{job_id} for status.",
    )


@app.get(
    "/run-team/{job_id}",
    response_model=JobStatusResponse,
    summary="Get job status",
    description="Poll this endpoint for job progress and results.",
)
def get_job_status(job_id: str) -> JobStatusResponse:
    """Get the status of a run-team job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    raw_failed = data.get("failed_tasks") or []
    failed_tasks = [
        FailedTaskDetail(
            task_id=ft.get("task_id", ""),
            title=ft.get("title", ""),
            reason=ft.get("reason", ""),
        )
        for ft in raw_failed
    ]

    return JobStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        repo_path=data.get("repo_path"),
        requirements_title=data.get("requirements_title"),
        architecture_overview=data.get("architecture_overview"),
        current_task=data.get("current_task"),
        task_results=data.get("task_results", []),
        task_ids=data.get("execution_order", []),
        progress=data.get("progress"),
        error=data.get("error"),
        failed_tasks=failed_tasks,
    )


@app.post(
    "/run-team/{job_id}/retry-failed",
    response_model=RetryResponse,
    summary="Retry failed tasks",
    description="Re-run only the tasks that failed in a previous job run. "
    "Use when status is completed, failed, or paused_llm_limit. "
    "When paused_llm_limit (Ollama weekly usage limit exceeded), call after the weekly limit resets to resume.",
)
def retry_failed_tasks(job_id: str) -> RetryResponse:
    """Retry the failed tasks from a previous job run."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    status = data.get("status")
    if status == "running":
        raise HTTPException(status_code=409, detail="Job is still running")

    failed_tasks = data.get("failed_tasks") or []
    if not failed_tasks:
        raise HTTPException(status_code=400, detail="No failed tasks to retry")

    failed_ids = [ft.get("task_id", "") for ft in failed_tasks]

    thread = threading.Thread(target=_run_retry_background, args=(job_id,))
    thread.daemon = True
    thread.start()

    return RetryResponse(
        job_id=job_id,
        status="running",
        retrying_tasks=failed_ids,
        message=f"Retrying {len(failed_ids)} failed tasks. Poll GET /run-team/{job_id} for status.",
    )


@app.post("/clarification/sessions", response_model=ClarificationResponse)
def create_clarification_session(request: ClarificationCreateRequest) -> ClarificationResponse:
    """Create a clarification session from initial spec text."""
    session = clarification_store.create_session(request.spec_text)
    return ClarificationResponse(
        session_id=session.session_id,
        assistant_message=session.turns[-1].message,
        open_questions=session.open_questions,
        assumptions=session.assumptions,
        done_clarifying=False,
    )


@app.post("/clarification/sessions/{session_id}/messages", response_model=ClarificationResponse)
def send_clarification_message(session_id: str, request: ClarificationMessageRequest) -> ClarificationResponse:
    """Append a user message and return next question or completion."""
    session = clarification_store.add_user_message(session_id, request.message)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return ClarificationResponse(
        session_id=session.session_id,
        assistant_message=session.turns[-1].message,
        open_questions=session.open_questions,
        assumptions=session.assumptions,
        done_clarifying=session.status == "completed",
        refined_spec=session.refined_spec,
    )


@app.get("/clarification/sessions/{session_id}", response_model=ClarificationSessionResponse)
def get_clarification_session(session_id: str) -> ClarificationSessionResponse:
    """Get full clarification session transcript and state."""
    session = clarification_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return ClarificationSessionResponse(
        session_id=session.session_id,
        spec_text=session.spec_text,
        status=session.status,
        created_at=session.created_at,
        clarification_round=session.clarification_round,
        max_rounds=session.max_rounds,
        confidence_score=session.confidence_score,
        open_questions=session.open_questions,
        assumptions=session.assumptions,
        refined_spec=session.refined_spec,
        turns=[{"role": t.role, "message": t.message, "timestamp": t.timestamp} for t in session.turns],
    )


@app.get("/execution/tasks")
def get_execution_tasks() -> Dict[str, Any]:
    """Get task status, plan progress, loop metrics, and timing metrics."""
    return execution_tracker.snapshot()


@app.get("/execution/stream")
def stream_execution_events() -> StreamingResponse:
    """SSE endpoint for execution updates."""

    def event_generator():
        index = 0
        for _ in range(300):
            events = execution_tracker.events_since(index)
            if events:
                for event in events:
                    yield f"data: {json.dumps(event)}\n\n"
                index += len(events)
            else:
                yield ": keepalive\n\n"
            time.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
