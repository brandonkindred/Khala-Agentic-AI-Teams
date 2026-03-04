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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Path setup for imports when run as uvicorn from project root
import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_arch_dir = _team_dir / "architect-agents"
if _arch_dir.exists() and str(_arch_dir) not in sys.path:
    sys.path.insert(0, str(_arch_dir))

from spec_parser import validate_work_path
from software_engineering_team.shared.execution_tracker import execution_tracker
from software_engineering_team.shared.job_store import (
    JOB_STATUS_AGENT_CRASH,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    is_cancel_requested,
    list_jobs,
    request_cancel,
    update_job,
    submit_answers as store_submit_answers,
)

from software_engineering_team.shared.logging_config import setup_logging

setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Software Engineering Team API",
    description="Async API: POST /run-team with work folder path returns job_id. "
    "GET /run-team/{job_id} polls status. Tech Lead orchestrates the full pipeline.",
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunTeamRequest(BaseModel):
    """Request body for the run-team endpoint."""

    repo_path: str = Field(
        ...,
        max_length=4096,
        description="Local filesystem path to the folder where work will be saved. Must contain a spec: at root (initial_spec.md or spec.md) or under plan/ or plan/product_analysis/ (e.g. validated_spec.md, updated_spec_vN.md). Does not need to be a git repository.",
    )


class RunTeamResponse(BaseModel):
    """Response from POST /run-team."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running", description="Initial status.")
    message: str = Field(default="Orchestrator started. Poll GET /run-team/{job_id} for status.")


class RunningJobSummary(BaseModel):
    """Summary of a single job for the running jobs list."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(..., description="pending or running.")
    repo_path: Optional[str] = Field(None, description="Path to the repo.")
    job_type: str = Field(
        default="run_team",
        description="run_team, backend_code_v2, or planning_v2.",
    )
    created_at: Optional[str] = Field(None, description="ISO timestamp when job was created.")


class RunningJobsResponse(BaseModel):
    """Response from GET /run-team/jobs (list of running/pending jobs)."""

    jobs: List[RunningJobSummary] = Field(default_factory=list, description="Running or pending jobs.")


class FailedTaskDetail(BaseModel):
    """Detail about a single failed task."""

    task_id: str = Field(..., description="ID of the failed task.")
    title: str = Field(default="", description="Task title.")
    reason: str = Field(default="", description="Why the task failed.")


class TaskStateEntry(BaseModel):
    """Per-task execution state for tracking panel / graph."""

    status: str = Field(..., description="pending, in_progress, done, failed")
    assignee: str = Field(..., description="Team: backend-code-v2, backend, frontend, git_setup, devops")
    title: Optional[str] = Field(None, description="Task title.")
    dependencies: List[str] = Field(default_factory=list, description="Task IDs this task depends on.")
    started_at: Optional[str] = Field(None, description="ISO timestamp when task started.")
    finished_at: Optional[str] = Field(None, description="ISO timestamp when task finished.")
    error: Optional[str] = Field(None, description="Error message if failed.")
    initiative_id: Optional[str] = Field(None, description="Parent initiative ID from planning hierarchy.")
    epic_id: Optional[str] = Field(None, description="Parent epic ID from planning hierarchy.")
    story_id: Optional[str] = Field(None, description="Parent story ID from planning hierarchy.")


class TeamProgressEntry(BaseModel):
    """Per-team progress when multiple teams run in parallel."""

    current_phase: Optional[str] = Field(None, description="e.g. planning, execution, review (backend-code-v2).")
    progress: Optional[int] = Field(None, description="0-100 completion for this team.")
    current_task_id: Optional[str] = Field(None, description="Task ID currently being executed by this team.")
    current_microtask: Optional[str] = Field(None, description="Title of the currently executing microtask.")
    current_microtask_phase: Optional[str] = Field(
        None,
        description="Current phase of the microtask: coding, code_review, qa_testing, security_testing, documentation, or completed.",
    )
    phase_detail: Optional[str] = Field(
        None,
        description="Human-readable detail about what's happening within the current phase.",
    )
    current_microtask_index: Optional[int] = Field(None, description="1-based index of the currently executing microtask.")
    microtasks_completed: Optional[int] = Field(None, description="Number of microtasks completed.")
    microtasks_total: Optional[int] = Field(None, description="Total number of microtasks.")


class QuestionOption(BaseModel):
    """A selectable option for a pending question."""

    id: str = Field(..., description="Unique identifier for this option.")
    label: str = Field(..., description="Display text for this option.")


class PendingQuestion(BaseModel):
    """A question awaiting user response during job execution."""

    id: str = Field(..., description="Unique identifier for this question.")
    question_text: str = Field(..., description="The question to display to the user.")
    context: Optional[str] = Field(None, description="Additional context or explanation.")
    options: List[QuestionOption] = Field(
        default_factory=list,
        description="Selectable answer options. Always includes an 'other' option automatically.",
    )
    required: bool = Field(default=True, description="Whether this question must be answered.")
    source: str = Field(
        default="planning",
        description="Source of the question: planning, tech_lead, execution, etc.",
    )


class JobStatusResponse(BaseModel):
    """Response from GET /run-team/{job_id}."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(
        ...,
        description="pending, running, completed, failed, paused_llm_limit (Ollama weekly limit; call retry-failed after reset), or paused_llm_connectivity (LLM unreachable; call resume-after-llm-check when connectivity is restored).",
    )
    repo_path: Optional[str] = Field(None, description="Path to the repo.")
    requirements_title: Optional[str] = Field(None, description="Parsed project title.")
    architecture_overview: Optional[str] = Field(None, description="Architecture overview.")
    current_task: Optional[str] = Field(None, description="Current task being executed.")
    status_text: Optional[str] = Field(None, description="Human-readable status message describing current activity.")
    task_results: list = Field(default_factory=list, description="Completed task results.")
    task_ids: list = Field(default_factory=list, description="Task IDs in execution order.")
    progress: Optional[int] = Field(None, description="Progress percentage.")
    error: Optional[str] = Field(None, description="Error message if failed.")
    failed_tasks: List[FailedTaskDetail] = Field(
        default_factory=list,
        description="Details about tasks that failed, including the reason for failure.",
    )
    phase: Optional[str] = Field(
        None,
        description="Job-level phase: planning, execution, or completed.",
    )
    task_states: Optional[Dict[str, TaskStateEntry]] = Field(
        None,
        description="Per-task state (status, assignee, etc.) for execution tracking graph.",
    )
    team_progress: Optional[Dict[str, TeamProgressEntry]] = Field(
        None,
        description="Per-team progress when multiple teams run in parallel.",
    )
    pending_questions: List[PendingQuestion] = Field(
        default_factory=list,
        description="Questions awaiting user response before job can proceed.",
    )
    waiting_for_answers: bool = Field(
        default=False,
        description="True when job is blocked waiting for user to answer pending questions.",
    )
    planning_subprocess: Optional[str] = Field(
        None,
        description="Current subprocess within planning phase (planning, implementation, review, problem_solving, deliver).",
    )
    planning_completed_phases: List[str] = Field(
        default_factory=list,
        description="Completed subprocesses within the planning phase.",
    )
    analysis_subprocess: Optional[str] = Field(
        None,
        description="Current subprocess within product_analysis phase (spec_review, communicate, spec_update, spec_cleanup).",
    )
    analysis_completed_phases: List[str] = Field(
        default_factory=list,
        description="Completed subprocesses within the product_analysis phase.",
    )
    planning_hierarchy: Optional[Dict[str, Any]] = Field(
        None,
        description="Planning hierarchy with initiatives, epics, stories for work breakdown tree display.",
    )


class RetryResponse(BaseModel):
    """Response from POST /run-team/{job_id}/retry-failed."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(default="running", description="Status after retry start.")
    retrying_tasks: List[str] = Field(default_factory=list, description="Task IDs being retried.")
    message: str = Field(default="")


class AnswerSubmission(BaseModel):
    """A user's answer to a pending question."""

    question_id: str = Field(..., description="ID of the question being answered.")
    selected_option_id: Optional[str] = Field(
        None,
        description="ID of the selected option, or 'other' if custom text provided.",
    )
    other_text: Optional[str] = Field(
        None,
        description="Custom text when 'other' option is selected.",
    )


class SubmitAnswersRequest(BaseModel):
    """Request body for submitting answers to pending questions."""

    answers: List[AnswerSubmission] = Field(
        ...,
        description="List of answers to submit.",
    )


class ArchitectDesignRequest(BaseModel):
    """Request body for the architect/design endpoint."""

    spec: str = Field(..., description="Product/engineering specification text")
    use_llm: bool = Field(
        default=False,
        description="Use LLM for spec parsing (slower but higher quality); default uses heuristic",
    )


class ArchitectDesignResponse(BaseModel):
    """Response from POST /architect/design."""

    overview: str = Field(..., description="High-level architecture overview")
    architecture_document: str = Field(default="", description="Full markdown architecture document")
    components: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Architecture components (name, type, description, technology, etc.)",
    )
    diagrams: Dict[str, str] = Field(
        default_factory=dict,
        description="Mermaid diagram code keyed by diagram name",
    )
    decisions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Architecture decision records",
    )
    tenancy_model: str = Field(default="", description="Tenancy model")
    reliability_model: str = Field(default="", description="Reliability model")
    summary: str = Field(default="", description="Architecture summary")


def _run_orchestrator_background(
    job_id: str,
    repo_path: str,
    *,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
) -> None:
    """Run orchestrator in background thread."""
    try:
        from orchestrator import run_orchestrator
        run_orchestrator(
            job_id,
            repo_path,
            spec_content_override=spec_content_override,
            resolved_questions_override=resolved_questions_override,
            planning_only=planning_only,
        )
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
    create_job(job_id, str(repo_path), job_type="run_team")

    thread = threading.Thread(
        target=_run_orchestrator_background,
        args=(job_id, str(repo_path)),
    )
    thread.daemon = True
    thread.start()

    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Orchestrator started. Poll GET /run-team/{job_id} for status.",
    )


def _parse_task_states(raw: Any) -> Optional[Dict[str, TaskStateEntry]]:
    """Convert raw task_states dict from job store to TaskStateEntry map."""
    if not raw or not isinstance(raw, dict):
        return None
    result: Dict[str, TaskStateEntry] = {}
    for task_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            result[task_id] = TaskStateEntry(
                status=entry.get("status", "pending"),
                assignee=entry.get("assignee", "unknown"),
                title=entry.get("title"),
                dependencies=entry.get("dependencies") or [],
                started_at=entry.get("started_at"),
                finished_at=entry.get("finished_at"),
                error=entry.get("error"),
            )
        except Exception:
            continue
    return result if result else None


def _parse_team_progress(raw: Any) -> Optional[Dict[str, TeamProgressEntry]]:
    """Convert raw team_progress dict from job store to TeamProgressEntry map."""
    if not raw or not isinstance(raw, dict):
        return None
    result: Dict[str, TeamProgressEntry] = {}
    for team_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            result[team_id] = TeamProgressEntry(
                current_phase=entry.get("current_phase"),
                progress=entry.get("progress"),
                current_task_id=entry.get("current_task_id"),
                current_microtask=entry.get("current_microtask"),
                current_microtask_phase=entry.get("current_microtask_phase"),
                phase_detail=entry.get("phase_detail"),
                current_microtask_index=entry.get("current_microtask_index"),
                microtasks_completed=entry.get("microtasks_completed"),
                microtasks_total=entry.get("microtasks_total"),
            )
        except Exception:
            continue
    return result if result else None


def _coerce_progress(value: Any) -> Optional[int]:
    """Coerce progress to int or None for JobStatusResponse (JSON may give float)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@app.get(
    "/run-team/jobs",
    response_model=RunningJobsResponse,
    summary="List running jobs",
    description="Returns all jobs with status pending or running. Used by the UI to show a monitoring panel.",
)
def get_running_jobs() -> RunningJobsResponse:
    """List running and pending jobs."""
    raw = list_jobs(running_only=True)
    jobs = [
        RunningJobSummary(
            job_id=item["job_id"],
            status=item["status"],
            repo_path=item.get("repo_path"),
            job_type=item.get("job_type") or "run_team",
            created_at=item.get("created_at"),
        )
        for item in raw
    ]
    # Sort by created_at descending (most recent first)
    jobs.sort(key=lambda j: j.created_at or "", reverse=True)
    return RunningJobsResponse(jobs=jobs)


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
            task_id=str(ft.get("task_id", "")),
            title=str(ft.get("title", "")) if ft.get("title") is not None else "",
            reason=str(ft.get("reason", "")) if ft.get("reason") is not None else "",
        )
        for ft in raw_failed
        if isinstance(ft, dict)
    ]

    execution_order = data.get("execution_order")
    task_ids = list(execution_order) if isinstance(execution_order, list) else []

    task_states_parsed = _parse_task_states(data.get("task_states"))
    team_progress_parsed = _parse_team_progress(data.get("team_progress"))

    raw_pending_questions = data.get("pending_questions", [])
    pending_questions_parsed = []
    for pq in raw_pending_questions:
        if isinstance(pq, dict):
            options = [
                QuestionOption(**opt) if isinstance(opt, dict) else opt
                for opt in pq.get("options", [])
            ]
            pending_questions_parsed.append(
                PendingQuestion(
                    id=pq.get("id", ""),
                    question_text=pq.get("question_text", ""),
                    context=pq.get("context"),
                    options=options,
                    required=pq.get("required", True),
                    source=pq.get("source", "planning"),
                )
            )

    payload: Dict[str, Any] = {
        "job_id": str(job_id),
        "status": str(data.get("status", JOB_STATUS_PENDING)),
        "repo_path": data.get("repo_path"),
        "requirements_title": data.get("requirements_title"),
        "architecture_overview": data.get("architecture_overview"),
        "current_task": data.get("current_task"),
        "task_results": data.get("task_results") if isinstance(data.get("task_results"), list) else [],
        "task_ids": task_ids,
        "progress": _coerce_progress(data.get("progress")),
        "error": data.get("error"),
        "failed_tasks": [ft.model_dump() for ft in failed_tasks],
        "phase": data.get("phase"),
        "task_states": {k: v.model_dump() for k, v in task_states_parsed.items()} if task_states_parsed else None,
        "team_progress": {k: v.model_dump() for k, v in team_progress_parsed.items()} if team_progress_parsed else None,
        "pending_questions": [pq.model_dump() for pq in pending_questions_parsed],
        "waiting_for_answers": bool(data.get("waiting_for_answers", False)),
        "planning_subprocess": data.get("planning_subprocess"),
        "planning_completed_phases": data.get("planning_completed_phases") or [],
        "analysis_subprocess": data.get("analysis_subprocess"),
        "analysis_completed_phases": data.get("analysis_completed_phases") or [],
        "planning_hierarchy": data.get("planning_hierarchy"),
    }
    return JobStatusResponse.model_validate(payload)


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


class CancelJobResponse(BaseModel):
    """Response from POST /run-team/{job_id}/cancel."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(default="cancelled", description="New status after cancellation.")
    message: str = Field(default="Job cancellation requested.")


@app.post(
    "/run-team/{job_id}/cancel",
    response_model=CancelJobResponse,
    summary="Cancel a running job",
    description="Request cancellation for a running or pending job. Sets a cancellation flag that running agents "
    "check cooperatively and exit gracefully. Returns 200 if cancellation was requested, 404 if job not found, "
    "400 if job is already in a terminal state (completed, failed, or cancelled).",
)
def cancel_job(job_id: str) -> CancelJobResponse:
    """Request cancellation for a job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    current_status = data.get("status", JOB_STATUS_PENDING)
    terminal_statuses = (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)
    if current_status in terminal_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {current_status}. Cannot cancel.",
        )

    success = request_cancel(job_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Failed to request cancellation. Job may have changed state.",
        )

    return CancelJobResponse(
        job_id=job_id,
        status="cancelled",
        message="Job cancellation requested. Running agents will stop at the next checkpoint.",
    )


RESUMABLE_STATUSES = (JOB_STATUS_PENDING, JOB_STATUS_RUNNING, JOB_STATUS_AGENT_CRASH)


@app.post(
    "/run-team/{job_id}/resume",
    response_model=RunTeamResponse,
    summary="Resume an interrupted job",
    description="Re-start the orchestrator for a run_team job that was interrupted (e.g. server halt or runtime error). "
    "Allowed when status is pending, running, or agent_crash. Use after server restart to re-initiate the job; "
    "poll GET /run-team/{job_id} for status.",
)
def resume_run_team_job(job_id: str) -> RunTeamResponse:
    """Resume a run_team job by re-starting the orchestrator. Use after server restart or when the job appears stuck."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")

    job_type = data.get("job_type")
    if job_type is not None and job_type != "run_team":
        raise HTTPException(
            status_code=400,
            detail=f"Only run_team jobs can be resumed via this endpoint (job_type={job_type}).",
        )

    status = data.get("status", JOB_STATUS_PENDING)
    if status not in RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be resumed (status={status}). Resume is only allowed for pending, running, or agent_crash.",
        )

    repo_path = data.get("repo_path")
    if not repo_path:
        raise HTTPException(status_code=400, detail="Job has no repo_path; cannot resume.")

    try:
        validate_work_path(repo_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    update_job(
        job_id,
        status=JOB_STATUS_RUNNING,
        error=None,
        agent_crash_details=None,
    )

    thread = threading.Thread(
        target=_run_orchestrator_background,
        args=(job_id, str(repo_path)),
        daemon=True,
    )
    thread.start()

    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Job resumed. Poll GET /run-team/{job_id} for status.",
    )


@app.post(
    "/run-team/{job_id}/resume-after-llm-check",
    response_model=RetryResponse,
    summary="Resume after LLM connectivity check",
    description="Use when the job status is paused_llm_connectivity (frontend could not reach the LLM after retries). "
    "After the user has verified LLM connectivity, call this endpoint to set status to running and retry the failed task(s). "
    "Same retry flow as retry-failed; poll GET /run-team/{job_id} for status.",
)
def resume_after_llm_check(job_id: str) -> RetryResponse:
    """Resume a job paused due to LLM connectivity by retrying the failed tasks."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    status = data.get("status")
    if status != JOB_STATUS_PAUSED_LLM_CONNECTIVITY:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not paused for LLM connectivity (status={status}). Use this endpoint only when status is {JOB_STATUS_PAUSED_LLM_CONNECTIVITY}.",
        )

    failed_tasks = data.get("failed_tasks") or []
    failed_ids = [ft.get("task_id", "") for ft in failed_tasks]

    update_job(job_id, status="running", error=None)

    thread = threading.Thread(target=_run_retry_background, args=(job_id,))
    thread.daemon = True
    thread.start()

    return RetryResponse(
        job_id=job_id,
        status="running",
        retrying_tasks=failed_ids,
        message="Resumed after LLM connectivity check. Poll GET /run-team/{job_id} for status.",
    )


@app.post(
    "/run-team/{job_id}/answers",
    response_model=JobStatusResponse,
    summary="Submit answers to pending questions",
    description="Submit user answers to pending questions. The job will resume once all required questions are answered. "
    "Each answer can select a predefined option or provide custom 'other' text.",
)
def submit_pending_answers(job_id: str, request: SubmitAnswersRequest) -> JobStatusResponse:
    """Submit answers to pending questions and resume job execution."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not data.get("waiting_for_answers"):
        raise HTTPException(
            status_code=400,
            detail="Job is not waiting for answers.",
        )

    pending_questions = data.get("pending_questions", [])
    if not pending_questions:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")

    pending_ids = {q["id"] for q in pending_questions}
    required_ids = {q["id"] for q in pending_questions if q.get("required", True)}
    answered_ids = {a.question_id for a in request.answers}

    missing_required = required_ids - answered_ids
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail=f"Missing answers for required questions: {', '.join(sorted(missing_required))}",
        )

    invalid_ids = answered_ids - pending_ids
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown question IDs: {', '.join(sorted(invalid_ids))}",
        )

    for answer in request.answers:
        if answer.selected_option_id == "other" and not answer.other_text:
            raise HTTPException(
                status_code=400,
                detail=f"Question {answer.question_id}: 'other' selected but no text provided.",
            )

    answers_dicts = [
        {
            "question_id": a.question_id,
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]
    store_submit_answers(job_id, answers_dicts)

    updated_data = get_job(job_id)
    return JobStatusResponse(
        job_id=job_id,
        status=updated_data.get("status", "running"),
        repo_path=updated_data.get("repo_path"),
        requirements_title=updated_data.get("requirements_title"),
        architecture_overview=updated_data.get("architecture_overview"),
        current_task=updated_data.get("current_task"),
        status_text=updated_data.get("status_text"),
        task_results=updated_data.get("task_results", []),
        task_ids=updated_data.get("execution_order", []),
        progress=updated_data.get("progress"),
        error=updated_data.get("error"),
        failed_tasks=[
            FailedTaskDetail(**ft)
            for ft in updated_data.get("failed_tasks", [])
        ],
        phase=updated_data.get("phase"),
        task_states={
            k: TaskStateEntry(**v)
            for k, v in (updated_data.get("task_states") or {}).items()
        } if updated_data.get("task_states") else None,
        team_progress={
            k: TeamProgressEntry(**v)
            for k, v in (updated_data.get("team_progress") or {}).items()
        } if updated_data.get("team_progress") else None,
        pending_questions=[
            PendingQuestion(**q)
            for q in updated_data.get("pending_questions", [])
        ],
        waiting_for_answers=updated_data.get("waiting_for_answers", False),
        planning_subprocess=updated_data.get("planning_subprocess"),
        planning_completed_phases=updated_data.get("planning_completed_phases") or [],
        analysis_subprocess=updated_data.get("analysis_subprocess"),
        analysis_completed_phases=updated_data.get("analysis_completed_phases") or [],
        planning_hierarchy=updated_data.get("planning_hierarchy"),
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


@app.post(
    "/architect/design",
    response_model=ArchitectDesignResponse,
    summary="Generate architecture from spec",
    description="Parse spec, run Architecture Expert agent, return architecture documents and diagrams. "
    "Uses heuristic spec parsing by default; set use_llm=true for LLM-based parsing.",
)
def architect_design(request: ArchitectDesignRequest) -> ArchitectDesignResponse:
    """Generate software architecture from a product specification."""
    try:
        from architecture_expert import ArchitectureExpertAgent
        from architecture_expert.models import ArchitectureInput
        from spec_parser import parse_spec_with_llm
        from software_engineering_team.shared.llm import get_llm_for_agent
    except ImportError as e:
        logger.exception("Failed to import architect dependencies")
        raise HTTPException(status_code=500, detail=f"Architect agent unavailable: {e}") from e

    if not request.spec or not request.spec.strip():
        raise HTTPException(status_code=400, detail="Spec text is required")

    try:
        llm = get_llm_for_agent("architecture")
        requirements = parse_spec_with_llm(request.spec.strip(), llm)

        arch_agent = ArchitectureExpertAgent(get_llm_for_agent("architecture"))
        arch_input = ArchitectureInput(requirements=requirements)
        arch_output = arch_agent.run(arch_input)
        architecture = arch_output.architecture

        components = [
            c.model_dump() if hasattr(c, "model_dump") else c.dict()
            for c in architecture.components
        ]

        return ArchitectDesignResponse(
            overview=architecture.overview,
            architecture_document=architecture.architecture_document or "",
            components=components,
            diagrams=architecture.diagrams or {},
            decisions=architecture.decisions or [],
            tenancy_model=getattr(architecture, "tenancy_model", "") or "",
            reliability_model=getattr(architecture, "reliability_model", "") or "",
            summary=arch_output.summary or "",
        )
    except Exception as e:
        logger.exception("Architect design failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Backend-Code-V2 endpoints
# ---------------------------------------------------------------------------

class BackendCodeV2TaskInput(BaseModel):
    """Task input for backend-code-v2."""

    id: str = Field(default="", description="Task ID (auto-generated if empty)")
    title: str = Field(default="", description="Short task title")
    description: str = Field(default="", description="Detailed description")
    requirements: str = Field(default="", description="Technical requirements")
    acceptance_criteria: List[str] = Field(default_factory=list, description="Acceptance criteria list")


class BackendCodeV2RunRequest(BaseModel):
    """Request body for POST /backend-code-v2/run."""

    task: BackendCodeV2TaskInput = Field(..., description="Task to implement")
    repo_path: str = Field(..., description="Local path to the repository")
    architecture: Optional[str] = Field(None, description="Optional architecture overview")


class BackendCodeV2RunResponse(BaseModel):
    """Response from POST /backend-code-v2/run."""

    job_id: str = Field(..., description="Job ID for polling status")
    status: str = Field(default="running")
    message: str = Field(default="")


class BackendCodeV2MicrotaskStatus(BaseModel):
    """Status of a single microtask."""

    id: str = Field(default="")
    title: str = Field(default="")
    status: str = Field(default="pending")


class BackendCodeV2StatusResponse(BaseModel):
    """Response from GET /backend-code-v2/status/{job_id}."""

    job_id: str = Field(...)
    status: str = Field(default="pending", description="pending, running, completed, failed")
    repo_path: Optional[str] = None
    current_phase: Optional[str] = None
    current_microtask: Optional[str] = None
    progress: int = Field(default=0, description="0-100 completion percentage")
    microtasks_completed: int = Field(default=0)
    microtasks_total: int = Field(default=0)
    completed_phases: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    summary: Optional[str] = None


# ---------------------------------------------------------------------------
# Frontend-Code-V2 endpoints
# ---------------------------------------------------------------------------

class FrontendCodeV2TaskInput(BaseModel):
    """Task input for frontend-code-v2."""

    id: str = Field(default="", description="Task ID (auto-generated if empty)")
    title: str = Field(default="", description="Short task title")
    description: str = Field(default="", description="Detailed description")
    requirements: str = Field(default="", description="Technical requirements")
    acceptance_criteria: List[str] = Field(default_factory=list, description="Acceptance criteria list")


class FrontendCodeV2RunRequest(BaseModel):
    """Request body for POST /frontend-code-v2/run."""

    task: FrontendCodeV2TaskInput = Field(..., description="Task to implement")
    repo_path: str = Field(..., description="Local path to the repository")
    architecture: Optional[str] = Field(None, description="Optional architecture overview")


class FrontendCodeV2RunResponse(BaseModel):
    """Response from POST /frontend-code-v2/run."""

    job_id: str = Field(..., description="Job ID for polling status")
    status: str = Field(default="running")
    message: str = Field(default="")


class FrontendCodeV2StatusResponse(BaseModel):
    """Response from GET /frontend-code-v2/status/{job_id}."""

    job_id: str = Field(...)
    status: str = Field(default="pending", description="pending, running, completed, failed")
    repo_path: Optional[str] = None
    current_phase: Optional[str] = None
    current_microtask: Optional[str] = None
    progress: int = Field(default=0, description="0-100 completion percentage")
    microtasks_completed: int = Field(default=0)
    microtasks_total: int = Field(default=0)
    completed_phases: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    summary: Optional[str] = None


def _run_frontend_code_v2_background(job_id: str, repo_path: str, task_dict: dict, architecture_overview: str) -> None:
    """Run frontend-code-v2 workflow in a background thread."""
    try:
        from pathlib import Path as _Path
        from frontend_code_v2_team import FrontendCodeV2TeamLead
        from software_engineering_team.shared.llm import get_llm_for_agent
        from software_engineering_team.shared.models import Task, TaskStatus, TaskType, SystemArchitecture
        import uuid as _uuid

        update_job(job_id, status="running")

        tid = task_dict.get("id") or f"fv2-{_uuid.uuid4().hex[:8]}"
        task = Task(
            id=tid,
            title=task_dict.get("title", ""),
            description=task_dict.get("description", ""),
            requirements=task_dict.get("requirements", ""),
            acceptance_criteria=task_dict.get("acceptance_criteria", []),
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
        )

        arch = SystemArchitecture(overview=architecture_overview) if architecture_overview else None

        team_lead = FrontendCodeV2TeamLead(get_llm_for_agent("frontend"))

        phase_order = ["setup", "planning", "execution", "review", "problem_solving", "documentation", "deliver"]

        def _job_updater(**kwargs):
            completed_phases = []
            current = kwargs.get("current_phase", "")
            for p in phase_order:
                if p == current:
                    break
                completed_phases.append(p)
            update_job(job_id, completed_phases=completed_phases, **kwargs)

        result = team_lead.run_workflow(
            repo_path=_Path(repo_path),
            task=task,
            architecture=arch,
            job_updater=_job_updater,
        )

        final_status = "completed" if result.success else "failed"
        update_job(
            job_id,
            status=final_status,
            progress=100 if result.success else (result.iterations_used * 20),
            summary=result.summary,
            error=result.failure_reason if not result.success else None,
            current_phase=result.current_phase.value if result.current_phase else "deliver",
        )
    except Exception as e:
        logger.exception("Frontend-code-v2 workflow failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


@app.post(
    "/frontend-code-v2/run",
    response_model=FrontendCodeV2RunResponse,
    summary="Run frontend-code-v2 agent team",
    description="Submit a task and repo path. Starts the frontend-code-v2 6-phase workflow in the background. "
    "Returns job_id immediately. Poll GET /frontend-code-v2/status/{job_id} for progress.",
)
def run_frontend_code_v2(request: FrontendCodeV2RunRequest) -> FrontendCodeV2RunResponse:
    """Start the frontend-code-v2 team on a task."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path does not exist or is not a directory: {request.repo_path}")

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type="frontend_code_v2")

    thread = threading.Thread(
        target=_run_frontend_code_v2_background,
        args=(
            job_id,
            request.repo_path,
            request.task.model_dump(),
            request.architecture or "",
        ),
    )
    thread.daemon = True
    thread.start()

    return FrontendCodeV2RunResponse(
        job_id=job_id,
        status="running",
        message="Frontend-code-v2 workflow started. Poll GET /frontend-code-v2/status/{job_id} for progress.",
    )


@app.get(
    "/frontend-code-v2/status/{job_id}",
    response_model=FrontendCodeV2StatusResponse,
    summary="Get frontend-code-v2 job status",
    description="Returns what is done, what is in progress, and overall completion percentage.",
)
def get_frontend_code_v2_status(job_id: str) -> FrontendCodeV2StatusResponse:
    """Get the status of a frontend-code-v2 job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return FrontendCodeV2StatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        repo_path=data.get("repo_path"),
        current_phase=data.get("current_phase"),
        current_microtask=data.get("current_microtask"),
        progress=data.get("progress", 0),
        microtasks_completed=data.get("microtasks_completed", 0),
        microtasks_total=data.get("microtasks_total", 0),
        completed_phases=data.get("completed_phases", []),
        error=data.get("error"),
        summary=data.get("summary"),
    )


# ---------------------------------------------------------------------------
# Planning-V2
# ---------------------------------------------------------------------------

class PlanningV2RunRequest(BaseModel):
    """Request body for POST /planning-v2/run."""

    spec_content: str = Field(..., description="Product/specification content")
    repo_path: str = Field(..., description="Local path where planning artifacts will be written")
    inspiration_content: Optional[str] = Field(None, description="Optional inspiration/moodboard content")


class PlanningV2RunResponse(BaseModel):
    """Response from POST /planning-v2/run."""

    job_id: str = Field(..., description="Job ID for polling status")
    status: str = Field(default="running")
    message: str = Field(default="")


class PlanningV2StatusResponse(BaseModel):
    """Response from GET /planning-v2/status/{job_id}."""

    job_id: str = Field(...)
    status: str = Field(default="pending", description="pending, running, completed, failed")
    repo_path: Optional[str] = None
    current_phase: Optional[str] = None
    progress: int = Field(default=0, description="0-100 completion percentage")
    completed_phases: List[str] = Field(default_factory=list)
    active_roles: List[str] = Field(default_factory=list, description="Roles active in current phase")
    error: Optional[str] = None
    summary: Optional[str] = None
    pending_questions: List[PendingQuestion] = Field(
        default_factory=list,
        description="Questions requiring user input before workflow can continue.",
    )
    waiting_for_answers: bool = Field(
        default=False,
        description="True if the workflow is paused waiting for user answers.",
    )


class PlanningV2ResultResponse(BaseModel):
    """Response from GET /planning-v2/result/{job_id}. Phase results when job has completed (or failed after some phases)."""

    job_id: str = Field(..., description="Job ID")
    status: str = Field(..., description="completed or failed")
    phase_results: Dict[str, Any] = Field(
        default_factory=dict,
        description="Phase outputs: spec_review_result, planning_result, implementation_result, review_result, problem_solving_result, deliver_result",
    )
    summary: Optional[str] = None
    error: Optional[str] = None


def _run_backend_code_v2_background(job_id: str, repo_path: str, task_dict: dict, architecture_overview: str) -> None:
    """Run backend-code-v2 workflow in a background thread."""
    try:
        from pathlib import Path as _Path
        from backend_code_v2_team import BackendCodeV2TeamLead
        from software_engineering_team.shared.llm import get_llm_for_agent
        from software_engineering_team.shared.models import Task, TaskStatus, TaskType, SystemArchitecture
        import uuid as _uuid

        update_job(job_id, status="running")

        tid = task_dict.get("id") or f"bv2-{_uuid.uuid4().hex[:8]}"
        task = Task(
            id=tid,
            title=task_dict.get("title", ""),
            description=task_dict.get("description", ""),
            requirements=task_dict.get("requirements", ""),
            acceptance_criteria=task_dict.get("acceptance_criteria", []),
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
        )

        arch = SystemArchitecture(overview=architecture_overview) if architecture_overview else None

        team_lead = BackendCodeV2TeamLead(get_llm_for_agent("backend"))

        phase_order = ["setup", "planning", "execution", "review", "problem_solving", "documentation", "deliver"]

        def _job_updater(**kwargs):
            completed_phases = []
            current = kwargs.get("current_phase", "")
            for p in phase_order:
                if p == current:
                    break
                completed_phases.append(p)
            update_job(job_id, completed_phases=completed_phases, **kwargs)

        result = team_lead.run_workflow(
            repo_path=_Path(repo_path),
            task=task,
            architecture=arch,
            job_updater=_job_updater,
        )

        final_status = "completed" if result.success else "failed"
        update_job(
            job_id,
            status=final_status,
            progress=100 if result.success else (result.iterations_used * 20),
            summary=result.summary,
            error=result.failure_reason if not result.success else None,
            current_phase=result.current_phase.value if result.current_phase else "deliver",
        )
    except Exception as e:
        logger.exception("Backend-code-v2 workflow failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


def _run_planning_v2_background(
    job_id: str,
    repo_path: str,
    spec_content: str,
    inspiration_content: str,
) -> None:
    """Run planning-v2 workflow in a background thread."""
    try:
        from pathlib import Path as _Path
        from planning_v2_team import PlanningV2TeamLead
        from planning_v2_team.models import Phase
        from software_engineering_team.shared.llm import get_llm_for_agent

        update_job(job_id, status="running")

        phase_order = [p.value for p in Phase]

        def _job_updater(**kwargs: Any) -> None:
            completed_phases = []
            current = kwargs.get("current_phase", "")
            for p in phase_order:
                if p == current:
                    break
                completed_phases.append(p)
            update_job(job_id, completed_phases=completed_phases, **kwargs)

        team_lead = PlanningV2TeamLead(get_llm_for_agent("backend"))
        result = team_lead.run_workflow(
            spec_content=spec_content,
            repo_path=_Path(repo_path),
            inspiration_content=inspiration_content or None,
            job_updater=_job_updater,
            job_id=job_id,
        )

        # Honor cancellation: if job was cancelled during execution, don't overwrite status
        if is_cancel_requested(job_id):
            logger.info("Planning-v2 workflow: cancellation detected, preserving cancelled state for job %s", job_id)
            return

        final_status = "completed" if result.success else "failed"
        phase_results: Dict[str, Any] = {}
        if result.spec_review_result is not None:
            phase_results["spec_review_result"] = result.spec_review_result.model_dump()
        if result.planning_result is not None:
            phase_results["planning_result"] = result.planning_result.model_dump()
        if result.implementation_result is not None:
            phase_results["implementation_result"] = result.implementation_result.model_dump()
        if result.review_result is not None:
            phase_results["review_result"] = result.review_result.model_dump()
        if result.problem_solving_result is not None:
            phase_results["problem_solving_result"] = result.problem_solving_result.model_dump()
        if result.deliver_result is not None:
            phase_results["deliver_result"] = result.deliver_result.model_dump()

        update_job(
            job_id,
            status=final_status,
            progress=100 if result.success else 90,
            summary=result.summary,
            error=result.failure_reason if not result.success else None,
            current_phase=Phase.DELIVER.value,
            phase_results=phase_results if phase_results else None,
        )
    except Exception as e:
        logger.exception("Planning-v2 workflow failed")
        # Honor cancellation: don't overwrite cancelled status with failed
        if not is_cancel_requested(job_id):
            update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        else:
            logger.info("Planning-v2 workflow: exception during cancelled job %s, preserving cancelled state", job_id)


@app.post(
    "/backend-code-v2/run",
    response_model=BackendCodeV2RunResponse,
    summary="Run backend-code-v2 agent team",
    description="Submit a task and repo path. Starts the backend-code-v2 5-phase workflow in the background. "
    "Returns job_id immediately. Poll GET /backend-code-v2/status/{job_id} for progress.",
)
def run_backend_code_v2(request: BackendCodeV2RunRequest) -> BackendCodeV2RunResponse:
    """Start the backend-code-v2 team on a task."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path does not exist or is not a directory: {request.repo_path}")

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type="backend_code_v2")

    thread = threading.Thread(
        target=_run_backend_code_v2_background,
        args=(
            job_id,
            request.repo_path,
            request.task.model_dump(),
            request.architecture or "",
        ),
    )
    thread.daemon = True
    thread.start()

    return BackendCodeV2RunResponse(
        job_id=job_id,
        status="running",
        message="Backend-code-v2 workflow started. Poll GET /backend-code-v2/status/{job_id} for progress.",
    )


@app.get(
    "/backend-code-v2/status/{job_id}",
    response_model=BackendCodeV2StatusResponse,
    summary="Get backend-code-v2 job status",
    description="Returns what is done, what is in progress, and overall completion percentage.",
)
def get_backend_code_v2_status(job_id: str) -> BackendCodeV2StatusResponse:
    """Get the status of a backend-code-v2 job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return BackendCodeV2StatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        repo_path=data.get("repo_path"),
        current_phase=data.get("current_phase"),
        current_microtask=data.get("current_microtask"),
        progress=data.get("progress", 0),
        microtasks_completed=data.get("microtasks_completed", 0),
        microtasks_total=data.get("microtasks_total", 0),
        completed_phases=data.get("completed_phases", []),
        error=data.get("error"),
        summary=data.get("summary"),
    )


@app.post(
    "/planning-v2/run",
    response_model=PlanningV2RunResponse,
    summary="Run planning-v2 agent",
    description="Submit spec and repo path. Starts the planning-v2 6-phase workflow in the background. "
    "Returns job_id. Poll GET /planning-v2/status/{job_id} for progress.",
)
def run_planning_v2(request: PlanningV2RunRequest) -> PlanningV2RunResponse:
    """Start the planning-v2 team."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"repo_path does not exist or is not a directory: {request.repo_path}",
        )

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type="planning_v2")

    thread = threading.Thread(
        target=_run_planning_v2_background,
        args=(
            job_id,
            request.repo_path,
            request.spec_content,
            request.inspiration_content or "",
        ),
    )
    thread.daemon = True
    thread.start()

    return PlanningV2RunResponse(
        job_id=job_id,
        status="running",
        message="Planning-v2 workflow started. Poll GET /planning-v2/status/{job_id} for progress.",
    )


@app.get(
    "/planning-v2/status/{job_id}",
    response_model=PlanningV2StatusResponse,
    summary="Get planning-v2 job status",
    description="Returns current phase, progress, completed phases, and active roles.",
)
def get_planning_v2_status(job_id: str) -> PlanningV2StatusResponse:
    """Get the status of a planning-v2 job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    pending_questions_raw = data.get("pending_questions", [])
    pending_questions = [
        PendingQuestion(
            id=q.get("id", ""),
            question_text=q.get("question_text", ""),
            context=q.get("context"),
            options=[
                QuestionOption(id=opt.get("id", ""), label=opt.get("label", ""))
                for opt in q.get("options", [])
            ],
            required=q.get("required", False),
        )
        for q in pending_questions_raw
    ]

    return PlanningV2StatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        repo_path=data.get("repo_path"),
        current_phase=data.get("current_phase"),
        progress=data.get("progress", 0),
        completed_phases=data.get("completed_phases", []),
        active_roles=data.get("active_roles", []),
        error=data.get("error"),
        summary=data.get("summary"),
        pending_questions=pending_questions,
        waiting_for_answers=data.get("waiting_for_answers", False),
    )


@app.post(
    "/planning-v2/{job_id}/answers",
    response_model=PlanningV2StatusResponse,
    summary="Submit answers to planning-v2 open questions",
    description="Submit user answers to open questions identified during spec review. "
    "The workflow will resume once all required questions are answered. "
    "Each answer can select a predefined option or provide custom 'other' text.",
)
def submit_planning_v2_answers(job_id: str, request: SubmitAnswersRequest) -> PlanningV2StatusResponse:
    """Submit answers to open questions and resume planning-v2 workflow."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") != "planning_v2":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for planning-v2 jobs.",
        )

    if not data.get("waiting_for_answers"):
        raise HTTPException(
            status_code=400,
            detail="Job is not waiting for answers.",
        )

    pending_questions = data.get("pending_questions", [])
    if not pending_questions:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")

    pending_ids = {q["id"] for q in pending_questions}
    required_ids = {q["id"] for q in pending_questions if q.get("required", True)}
    answered_ids = {a.question_id for a in request.answers}

    missing_required = required_ids - answered_ids
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail=f"Missing answers for required questions: {', '.join(sorted(missing_required))}",
        )

    invalid_ids = answered_ids - pending_ids
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown question IDs: {', '.join(sorted(invalid_ids))}",
        )

    for answer in request.answers:
        if answer.selected_option_id == "other" and not answer.other_text:
            raise HTTPException(
                status_code=400,
                detail=f"Question {answer.question_id}: 'other' selected but no text provided.",
            )

    answers_dicts = [
        {
            "question_id": a.question_id,
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]
    store_submit_answers(job_id, answers_dicts)

    updated_data = get_job(job_id)
    pending_questions_raw = updated_data.get("pending_questions", [])
    pending_questions_response = [
        PendingQuestion(
            id=q.get("id", ""),
            question_text=q.get("question_text", ""),
            context=q.get("context"),
            options=[
                QuestionOption(id=opt.get("id", ""), label=opt.get("label", ""))
                for opt in q.get("options", [])
            ],
            required=q.get("required", False),
        )
        for q in pending_questions_raw
    ]

    return PlanningV2StatusResponse(
        job_id=job_id,
        status=updated_data.get("status", "running"),
        repo_path=updated_data.get("repo_path"),
        current_phase=updated_data.get("current_phase"),
        progress=updated_data.get("progress", 0),
        completed_phases=updated_data.get("completed_phases", []),
        active_roles=updated_data.get("active_roles", []),
        error=updated_data.get("error"),
        summary=updated_data.get("summary"),
        pending_questions=pending_questions_response,
        waiting_for_answers=updated_data.get("waiting_for_answers", False),
    )


@app.get(
    "/planning-v2/result/{job_id}",
    response_model=PlanningV2ResultResponse,
    summary="Get planning-v2 job result",
    description="Returns phase results (spec_review, planning, implementation, review, problem_solving, deliver) when the job has finished. Returns 404 if job not found or result not yet available.",
)
def get_planning_v2_result(job_id: str) -> PlanningV2ResultResponse:
    """Get the phase results of a completed planning-v2 job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    status = data.get("status", JOB_STATUS_PENDING)
    if status in ("pending", "running"):
        raise HTTPException(status_code=404, detail="Result not yet available; job still in progress")
    phase_results = data.get("phase_results")
    if phase_results is None:
        phase_results = {}
    return PlanningV2ResultResponse(
        job_id=job_id,
        status=status,
        phase_results=phase_results,
        summary=data.get("summary"),
        error=data.get("error"),
    )


@app.get(
    "/planning-v2/jobs",
    response_model=RunningJobsResponse,
    summary="List planning-v2 jobs",
    description="Returns all planning-v2 jobs with status pending or running.",
)
def get_planning_v2_jobs() -> RunningJobsResponse:
    """List running and pending planning-v2 jobs."""
    raw = list_jobs(running_only=True, job_type="planning_v2")
    jobs = [
        RunningJobSummary(
            job_id=item["job_id"],
            status=item["status"],
            repo_path=item.get("repo_path"),
            job_type=item.get("job_type") or "planning_v2",
        )
        for item in raw
    ]
    return RunningJobsResponse(jobs=jobs)


# ---------------------------------------------------------------------------
# Auto-Answer Endpoints
# ---------------------------------------------------------------------------


class AutoAnswerRequest(BaseModel):
    """Request body for auto-answering a question."""

    spec_context: Optional[str] = Field(
        None,
        description="Additional context to help the LLM make a better choice.",
    )


class AutoAnswerResponse(BaseModel):
    """Response from auto-answering a question."""

    question_id: str = Field(..., description="ID of the question that was answered.")
    selected_option_id: str = Field(..., description="ID of the selected option.")
    selected_answer: str = Field(..., description="Text of the selected answer.")
    rationale: str = Field(..., description="Detailed explanation of why this choice was made.")
    confidence: float = Field(..., description="Confidence score (0.0-1.0) in this answer.")
    risks: List[str] = Field(default_factory=list, description="Potential risks of this choice.")
    applied: bool = Field(
        default=False,
        description="Whether the answer was auto-applied to the job.",
    )


@app.post(
    "/run-team/{job_id}/auto-answer/{question_id}",
    response_model=AutoAnswerResponse,
    summary="Auto-answer a pending question for run-team job",
    description="Use LLM to automatically answer a pending question based on industry best practices. "
    "The answer is NOT automatically applied - review the response and submit via /answers endpoint.",
)
def auto_answer_run_team_question(
    job_id: str,
    question_id: str,
    request: Optional[AutoAnswerRequest] = None,
) -> AutoAnswerResponse:
    """Auto-answer a pending question using LLM analysis."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") not in (None, "run_team"):
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for run-team jobs. Use /planning-v2/{job_id}/auto-answer/{question_id} for planning-v2 jobs.",
        )

    pending_questions = data.get("pending_questions", [])
    question_data = next(
        (q for q in pending_questions if q.get("id") == question_id), None
    )
    if not question_data:
        raise HTTPException(
            status_code=404,
            detail=f"Question {question_id} not found in pending questions.",
        )

    spec_content = _get_spec_content_for_job(data)
    additional_context = request.spec_context if request else None

    try:
        from product_requirements_analysis_agent import get_auto_answer_for_job
        from software_engineering_team.shared.llm import get_llm_for_agent

        llm = get_llm_for_agent("backend")
        result = get_auto_answer_for_job(
            llm=llm,
            job_id=job_id,
            question_id=question_id,
            spec_content=spec_content,
            additional_context=additional_context,
        )

        if not result:
            raise HTTPException(
                status_code=500,
                detail="Auto-answer failed to produce a result.",
            )

        return AutoAnswerResponse(
            question_id=result.question_id,
            selected_option_id=result.selected_option_id,
            selected_answer=result.selected_answer,
            rationale=result.rationale,
            confidence=result.confidence,
            risks=result.risks,
            applied=False,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer module not available: {e}",
        )
    except Exception as e:
        logger.exception("Auto-answer failed")
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer failed: {e}",
        )


@app.post(
    "/planning-v2/{job_id}/auto-answer/{question_id}",
    response_model=AutoAnswerResponse,
    summary="Auto-answer a pending question for planning-v2 job",
    description="Use LLM to automatically answer a pending question based on industry best practices. "
    "The answer is NOT automatically applied - review the response and submit via /answers endpoint.",
)
def auto_answer_planning_v2_question(
    job_id: str,
    question_id: str,
    request: Optional[AutoAnswerRequest] = None,
) -> AutoAnswerResponse:
    """Auto-answer a pending question using LLM analysis."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") != "planning_v2":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for planning-v2 jobs.",
        )

    pending_questions = data.get("pending_questions", [])
    question_data = next(
        (q for q in pending_questions if q.get("id") == question_id), None
    )
    if not question_data:
        raise HTTPException(
            status_code=404,
            detail=f"Question {question_id} not found in pending questions.",
        )

    spec_content = _get_spec_content_for_job(data)
    additional_context = request.spec_context if request else None

    try:
        from product_requirements_analysis_agent import get_auto_answer_for_job
        from software_engineering_team.shared.llm import get_llm_for_agent

        llm = get_llm_for_agent("backend")
        result = get_auto_answer_for_job(
            llm=llm,
            job_id=job_id,
            question_id=question_id,
            spec_content=spec_content,
            additional_context=additional_context,
        )

        if not result:
            raise HTTPException(
                status_code=500,
                detail="Auto-answer failed to produce a result.",
            )

        return AutoAnswerResponse(
            question_id=result.question_id,
            selected_option_id=result.selected_option_id,
            selected_answer=result.selected_answer,
            rationale=result.rationale,
            confidence=result.confidence,
            risks=result.risks,
            applied=False,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer module not available: {e}",
        )
    except Exception as e:
        logger.exception("Auto-answer failed")
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer failed: {e}",
        )


def _get_spec_content_for_job(data: Dict[str, Any]) -> str:
    """Get latest spec content for a job from its repo path. Returns '' if no spec file found."""
    repo_path = data.get("repo_path")
    if not repo_path:
        return ""

    repo = Path(repo_path)
    try:
        from spec_parser import get_latest_spec_content

        content = get_latest_spec_content(repo)
        return content[:12000]
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Product Analysis Endpoints
# ---------------------------------------------------------------------------


class ProductAnalysisRunRequest(BaseModel):
    """Request body for starting Product Requirements Analysis."""

    repo_path: str = Field(
        ...,
        max_length=4096,
        description="Local filesystem path to the folder. A spec can be at root (initial_spec.md or spec.md) or under plan/ or plan/product_analysis/ (e.g. validated_spec.md, updated_spec_vN.md).",
    )
    spec_content: Optional[str] = Field(
        None,
        max_length=500_000,
        description="Optional spec content. If not provided, the system loads the newest spec file whose name contains '_spec' "
        "(by modification time) from plan/product_analysis/, plan/, or root. Leave empty to use that file. "
        "If the agent needs more detail and the input was validated_spec.md, it is renamed to updated_spec_vN; "
        "subsequent updates use later versions.",
    )


class ProductAnalysisRunResponse(BaseModel):
    """Response from POST /product-analysis/run."""

    job_id: str = Field(..., description="Job ID for polling status.")
    status: str = Field(default="running", description="Initial status.")
    message: str = Field(default="Product analysis started. Poll GET /product-analysis/status/{job_id} for progress.")


class ProductAnalysisStatusResponse(BaseModel):
    """Response from GET /product-analysis/status/{job_id}."""

    job_id: str = Field(..., description="Job ID.")
    status: str = Field(..., description="pending, running, completed, or failed.")
    repo_path: Optional[str] = Field(None, description="Path to the repo.")
    current_phase: Optional[str] = Field(None, description="spec_review, communicate, spec_update, or spec_cleanup.")
    status_text: Optional[str] = Field(None, description="Human-readable status message describing current activity.")
    progress: int = Field(default=0, description="Progress percentage 0-100.")
    iterations: int = Field(default=0, description="Number of spec review iterations completed.")
    pending_questions: List[PendingQuestion] = Field(
        default_factory=list,
        description="Questions awaiting user response.",
    )
    waiting_for_answers: bool = Field(
        default=False,
        description="True when blocked waiting for user answers.",
    )
    error: Optional[str] = Field(None, description="Error message if failed.")
    summary: Optional[str] = Field(None, description="Summary of analysis results.")
    validated_spec_path: Optional[str] = Field(None, description="Path to validated spec file when complete.")


def _run_product_analysis_background(
    job_id: str,
    repo_path: str,
    spec_content: str,
    initial_spec_path: Optional[str] = None,
) -> None:
    """Run product analysis workflow in a background thread."""
    try:
        from pathlib import Path as _Path
        from product_requirements_analysis_agent import (
            AnalysisPhase,
            ProductRequirementsAnalysisAgent,
        )
        from software_engineering_team.shared.llm import get_llm_for_agent
        from spec_parser import gather_context_files

        update_job(job_id, status="running")

        def _job_updater(**kwargs: Any) -> None:
            update_job(job_id, **kwargs)

        # Gather context files for PRA agent
        context_files = gather_context_files(repo_path)
        if context_files:
            logger.info("Product analysis: Gathered %d context files", len(context_files))

        agent = ProductRequirementsAnalysisAgent(get_llm_for_agent("backend"))
        result = agent.run_workflow(
            spec_content=spec_content,
            repo_path=_Path(repo_path),
            job_id=job_id,
            job_updater=_job_updater,
            context_files=context_files,
            initial_spec_path=_Path(initial_spec_path) if initial_spec_path else None,
        )

        final_status = "completed" if result.success else "failed"
        update_job(
            job_id,
            status=final_status,
            progress=100 if result.success else 90,
            summary=result.summary,
            error=result.failure_reason if not result.success else None,
            current_phase=AnalysisPhase.SPEC_CLEANUP.value if result.success else result.current_phase.value if result.current_phase else None,
            iterations=result.iterations,
            validated_spec_path=result.validated_spec_path,
        )
    except Exception as e:
        logger.exception("Product analysis workflow failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


@app.post(
    "/product-analysis/run",
    response_model=ProductAnalysisRunResponse,
    summary="Start Product Requirements Analysis",
    description="Analyze product specification for completeness, identify gaps, and generate questions. "
    "If spec_content is omitted, the newest spec file (name contains '_spec') is loaded by modification time from plan/product_analysis/, plan/, or root. "
    "If the agent needs more detail and the input was validated_spec.md, it is renamed to updated_spec_vN and updates use subsequent versions. "
    "Returns job_id immediately. Poll GET /product-analysis/status/{job_id} for progress.",
)
def run_product_analysis(request: ProductAnalysisRunRequest) -> ProductAnalysisRunResponse:
    """Start the Product Requirements Analysis workflow."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"repo_path does not exist or is not a directory: {request.repo_path}",
        )

    spec_content = request.spec_content
    initial_spec_path = None
    if not spec_content:
        try:
            from spec_parser import get_newest_spec_path, get_newest_spec_content

            initial_spec_path = get_newest_spec_path(repo)
            spec_content = get_newest_spec_content(repo)
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=400,
                detail=f"No spec file found. {e}. Provide spec_content or add a spec file (e.g. initial_spec.md, plan/validated_spec.md).",
            ) from e

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type="product_analysis")

    thread = threading.Thread(
        target=_run_product_analysis_background,
        args=(job_id, request.repo_path, spec_content, initial_spec_path),
    )
    thread.daemon = True
    thread.start()

    return ProductAnalysisRunResponse(
        job_id=job_id,
        status="running",
        message="Product analysis started. Poll GET /product-analysis/status/{job_id} for progress.",
    )


@app.get(
    "/product-analysis/status/{job_id}",
    response_model=ProductAnalysisStatusResponse,
    summary="Get Product Analysis job status",
    description="Returns current phase, progress, pending questions, and completion status.",
)
def get_product_analysis_status(job_id: str) -> ProductAnalysisStatusResponse:
    """Get the status of a Product Analysis job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    pending_questions_raw = data.get("pending_questions", [])
    pending_questions = [
        PendingQuestion(
            id=q.get("id", ""),
            question_text=q.get("question_text", ""),
            context=q.get("context"),
            options=[
                QuestionOption(id=opt.get("id", ""), label=opt.get("label", ""))
                for opt in q.get("options", [])
            ],
            required=q.get("required", False),
            source=q.get("source", "spec_review"),
        )
        for q in pending_questions_raw
    ]

    return ProductAnalysisStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        repo_path=data.get("repo_path"),
        current_phase=data.get("current_phase"),
        status_text=data.get("status_text"),
        progress=data.get("progress", 0),
        iterations=data.get("iterations", 0),
        pending_questions=pending_questions,
        waiting_for_answers=data.get("waiting_for_answers", False),
        error=data.get("error"),
        summary=data.get("summary"),
        validated_spec_path=data.get("validated_spec_path"),
    )


@app.post(
    "/product-analysis/{job_id}/answers",
    response_model=ProductAnalysisStatusResponse,
    summary="Submit answers to Product Analysis open questions",
    description="Submit user answers to open questions identified during spec review.",
)
def submit_product_analysis_answers(job_id: str, request: SubmitAnswersRequest) -> ProductAnalysisStatusResponse:
    """Submit answers to open questions and resume Product Analysis workflow."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") != "product_analysis":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for product-analysis jobs.",
        )

    if not data.get("waiting_for_answers"):
        raise HTTPException(
            status_code=400,
            detail="Job is not waiting for answers.",
        )

    pending_questions = data.get("pending_questions", [])
    if not pending_questions:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")

    pending_ids = {q["id"] for q in pending_questions}
    required_ids = {q["id"] for q in pending_questions if q.get("required", True)}
    answered_ids = {a.question_id for a in request.answers}

    missing_required = required_ids - answered_ids
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail=f"Missing answers for required questions: {', '.join(sorted(missing_required))}",
        )

    invalid_ids = answered_ids - pending_ids
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown question IDs: {', '.join(sorted(invalid_ids))}",
        )

    answers_dicts = [
        {
            "question_id": a.question_id,
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]
    store_submit_answers(job_id, answers_dicts)

    return get_product_analysis_status(job_id)


@app.post(
    "/product-analysis/{job_id}/auto-answer/{question_id}",
    response_model=AutoAnswerResponse,
    summary="Auto-answer a pending question for Product Analysis job",
    description="Use LLM to automatically answer a pending question based on industry best practices.",
)
def auto_answer_product_analysis_question(
    job_id: str,
    question_id: str,
    request: Optional[AutoAnswerRequest] = None,
) -> AutoAnswerResponse:
    """Auto-answer a pending question using LLM analysis."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") != "product_analysis":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for product-analysis jobs.",
        )

    pending_questions = data.get("pending_questions", [])
    question_data = next(
        (q for q in pending_questions if q.get("id") == question_id), None
    )
    if not question_data:
        raise HTTPException(
            status_code=404,
            detail=f"Question {question_id} not found in pending questions.",
        )

    spec_content = _get_spec_content_for_job(data)
    additional_context = request.spec_context if request else None

    try:
        from product_requirements_analysis_agent import get_auto_answer_for_job
        from software_engineering_team.shared.llm import get_llm_for_agent

        llm = get_llm_for_agent("backend")
        result = get_auto_answer_for_job(
            llm=llm,
            job_id=job_id,
            question_id=question_id,
            spec_content=spec_content,
            additional_context=additional_context,
        )

        if not result:
            raise HTTPException(
                status_code=500,
                detail="Auto-answer failed to produce a result.",
            )

        return AutoAnswerResponse(
            question_id=result.question_id,
            selected_option_id=result.selected_option_id,
            selected_answer=result.selected_answer,
            rationale=result.rationale,
            confidence=result.confidence,
            risks=result.risks,
            applied=False,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer module not available: {e}",
        )
    except Exception as e:
        logger.exception("Auto-answer failed")
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer failed: {e}",
        )


@app.get(
    "/product-analysis/jobs",
    response_model=RunningJobsResponse,
    summary="List Product Analysis jobs",
    description="Returns all product-analysis jobs with status pending or running.",
)
def get_product_analysis_jobs() -> RunningJobsResponse:
    """List running and pending product-analysis jobs."""
    raw = list_jobs(running_only=True, job_type="product_analysis")
    jobs = [
        RunningJobSummary(
            job_id=item["job_id"],
            status=item["status"],
            repo_path=item.get("repo_path"),
            job_type=item.get("job_type") or "product_analysis",
        )
        for item in raw
    ]
    return RunningJobsResponse(jobs=jobs)


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
