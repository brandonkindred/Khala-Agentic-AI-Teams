"""
FastAPI application for the software engineering team.

Async API: POST /run-team returns job_id, GET /run-team/{job_id} polls status.
Tech Lead orchestrator runs in background.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Path setup for imports when run as uvicorn from project root
import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from spec_parser import validate_repo_path
from shared.job_store import JOB_STATUS_FAILED, JOB_STATUS_PENDING, create_job, get_job, update_job

from shared.logging_config import setup_logging

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Software Engineering Team API",
    description="Async API: POST /run-team with repo_path returns job_id. "
    "GET /run-team/{job_id} polls status. Tech Lead orchestrates the full pipeline.",
    version="0.2.0",
)


class RunTeamRequest(BaseModel):
    """Request body for the run-team endpoint."""

    repo_path: str = Field(
        ...,
        description="Local filesystem path to the git repository. Must contain initial_spec.md at the root.",
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
    status: str = Field(..., description="pending, running, completed, failed.")
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


def _run_orchestrator_background(job_id: str, repo_path: str) -> None:
    """Run orchestrator in background thread."""
    try:
        from orchestrator import run_orchestrator
        run_orchestrator(job_id, repo_path)
    except Exception as e:
        logger.exception("Orchestrator failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


@app.post(
    "/run-team",
    response_model=RunTeamResponse,
    summary="Start software engineering team",
    description="Validates repo, creates job, starts Tech Lead orchestrator in background. "
    "Returns job_id immediately. Poll GET /run-team/{job_id} for status.",
)
def run_team(request: RunTeamRequest) -> RunTeamResponse:
    """
    Start the software engineering team on a git repository.

    The repo must:
    - Exist and be a valid directory
    - Be a git repository (.git present)
    - Contain initial_spec.md at the root with the full project specification
    """
    try:
        repo_path = validate_repo_path(request.repo_path)
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
        message=f"Orchestrator started. Poll GET /run-team/{job_id} for status.",
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


class RetryResponse(BaseModel):
    """Response from POST /run-team/{job_id}/retry-failed."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(default="running", description="Status after retry start.")
    retrying_tasks: List[str] = Field(default_factory=list, description="Task IDs being retried.")
    message: str = Field(default="")


def _run_retry_background(job_id: str) -> None:
    """Run retry in background thread."""
    try:
        from orchestrator import run_failed_tasks
        run_failed_tasks(job_id)
    except Exception as e:
        logger.exception("Retry orchestrator failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


@app.post(
    "/run-team/{job_id}/retry-failed",
    response_model=RetryResponse,
    summary="Retry failed tasks",
    description="Re-run only the tasks that failed in a previous job run. "
    "The job must have completed with failed tasks.",
)
def retry_failed_tasks(job_id: str) -> RetryResponse:
    """
    Retry the failed tasks from a previous job run.

    Only works if the job has completed and has failed tasks.
    """
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


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
