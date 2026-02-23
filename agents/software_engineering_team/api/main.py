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
_arch_dir = _team_dir / "architect-agents"
if _arch_dir.exists() and str(_arch_dir) not in sys.path:
    sys.path.insert(0, str(_arch_dir))

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
        max_length=4096,
        description="Local filesystem path to the folder where work will be saved. Must contain initial_spec.md at the root. Does not need to be a git repository.",
    )
    clarification_session_id: Optional[str] = Field(
        None,
        max_length=256,
        description="Use refined_spec and resolved_questions from this clarification session",
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


class RePlanWithClarificationsRequest(BaseModel):
    """Request body for re-plan-with-clarifications endpoint."""

    clarification_session_id: str = Field(
        ...,
        description="Clarification session with refined_spec and resolved_questions to use for re-planning",
    )


class ClarificationCreateRequest(BaseModel):
    spec_text: str = Field(..., max_length=500_000, description="Initial product/engineering specification text.")


class ClarificationMessageRequest(BaseModel):
    message: str = Field(..., max_length=50_000, description="User clarification response message.")


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
    create_job(job_id, str(repo_path))

    spec_override: Optional[str] = None
    resolved_override: Optional[List[Dict[str, Any]]] = None
    if request.clarification_session_id:
        session = clarification_store.get_session(request.clarification_session_id)
        if not session:
            raise HTTPException(
                status_code=404,
                detail=f"Clarification session {request.clarification_session_id} not found",
            )
        spec_override = session.refined_spec or session.spec_text
        resolved_override = session.resolved_questions or []

    thread = threading.Thread(
        target=_run_orchestrator_background,
        args=(job_id, str(repo_path)),
        kwargs={
            "spec_content_override": spec_override,
            "resolved_questions_override": resolved_override,
        },
    )
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


def _run_replan_background(
    job_id: str,
    repo_path: str,
    spec_content_override: str,
    resolved_questions_override: List[Dict[str, Any]],
) -> None:
    """Run orchestrator in planning-only mode with clarification overrides."""
    try:
        from orchestrator import run_orchestrator
        run_orchestrator(
            job_id,
            repo_path,
            spec_content_override=spec_content_override,
            resolved_questions_override=resolved_questions_override,
            planning_only=True,
        )
    except Exception as e:
        logger.exception("Re-plan orchestrator failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


@app.post(
    "/run-team/{job_id}/re-plan-with-clarifications",
    response_model=RunTeamResponse,
    summary="Re-plan with clarifications",
    description="Re-run the planning phase (spec intake through conformance) using refined_spec and "
    "resolved_questions from a clarification session. Does not re-run execution. "
    "Use when user has provided clarification answers after a run completed.",
)
def re_plan_with_clarifications(job_id: str, request: RePlanWithClarificationsRequest) -> RunTeamResponse:
    """Re-run planning phase with clarification session data."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("status") == "running":
        raise HTTPException(status_code=409, detail="Job is still running")

    repo_path = data.get("repo_path")
    if not repo_path:
        raise HTTPException(status_code=400, detail="Job has no repo_path")

    session = clarification_store.get_session(request.clarification_session_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail=f"Clarification session {request.clarification_session_id} not found",
        )

    spec_override = session.refined_spec or session.spec_text
    resolved_override = session.resolved_questions or []

    thread = threading.Thread(
        target=_run_replan_background,
        args=(job_id, str(repo_path), spec_override, resolved_override),
    )
    thread.daemon = True
    thread.start()

    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Re-planning started. Poll GET /run-team/{job_id} for status.",
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
        from shared.llm import get_llm_for_agent
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
    spec_content: Optional[str] = Field(None, description="Optional project spec context")
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


def _run_backend_code_v2_background(job_id: str, repo_path: str, task_dict: dict, spec_content: str, architecture_overview: str) -> None:
    """Run backend-code-v2 workflow in a background thread."""
    try:
        from pathlib import Path as _Path
        from backend_code_v2_team import BackendCodeV2TeamLead
        from shared.llm import get_llm_for_agent
        from shared.models import Task, TaskStatus, TaskType, SystemArchitecture
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

        phase_order = ["planning", "execution", "review", "problem_solving", "deliver"]

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
            spec_content=spec_content or "",
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
    create_job(job_id, request.repo_path)

    thread = threading.Thread(
        target=_run_backend_code_v2_background,
        args=(
            job_id,
            request.repo_path,
            request.task.model_dump(),
            request.spec_content or "",
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


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
