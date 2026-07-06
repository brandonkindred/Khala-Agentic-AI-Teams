"""
FastAPI app for Planning team.

Mount at /api/planning: routes are /run, /status/{job_id}, /result/{job_id}, /jobs, /{job_id}/answers.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Ensure backend/agents is on path for job_service_client and software_engineering_team.shared.llm
_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from llm_service import get_client  # noqa: E402
from planning_team.models import (  # noqa: E402
    PlanningResultResponse,
    PlanningRunRequest,
    PlanningRunResponse,
    PlanningStatusResponse,
)
from planning_team.orchestrator import run_workflow  # noqa: E402
from planning_team.shared.job_store import (  # noqa: E402
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    list_jobs,
    mark_job_completed,
    mark_job_failed,
    update_job,
)
from planning_team.shared.workspace import (  # noqa: E402
    WorkspaceResolutionError,
    resolve_workspace,
)
from shared_app import create_team_app  # noqa: E402

logger = logging.getLogger(__name__)

app = create_team_app(
    service_name="planning-team",
    team_key="planning",
    title="Planning API",
    description="Client-facing discovery and requirements; PRD and handoff for dev/UI/UX",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SubmitAnswersRequest(BaseModel):
    """Submit answers to pending open questions."""

    answers: list[dict] = Field(
        ..., description="List of {question_id, selected_option_id?, other_text?}"
    )


def _get_llm():
    """Return a real LLMClient for the planning team.

    The discovery/requirements phases call ``llm.complete_text(...)`` and the spec
    digestion path uses the same client for per-section extraction plus a
    ``compact_text`` fallback (``.complete`` / ``.get_max_context_tokens``). A Strands
    ``Agent`` exposes none of these, so returning one made the phases silently fall
    back to default output. ``get_client`` returns an ``LLMClient`` that provides all
    three.
    """
    return get_client("planning")


def _run_workflow_background(
    job_id: str,
    repo_path: str,
    client_name: str | None,
    initial_brief: str | None,
    spec_content: str | None,
    use_product_analysis: bool,
    use_market_research: bool,
) -> None:
    def job_updater(
        current_phase: str | None = None,
        progress: int | None = None,
        status_text: str | None = None,
    ) -> None:
        updates = {}
        if current_phase is not None:
            updates["current_phase"] = current_phase
        if progress is not None:
            updates["progress"] = progress
        if status_text is not None:
            updates["status_text"] = status_text
        if updates:
            update_job(job_id, **updates)

    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = run_workflow(
            repo_path=repo_path,
            client_name=client_name,
            initial_brief=initial_brief,
            spec_content=spec_content,
            use_product_analysis=use_product_analysis,
            use_market_research=use_market_research,
            llm=_get_llm(),
            job_updater=job_updater,
        )
        if result.get("success"):
            mark_job_completed(
                job_id,
                handoff_package=result.get("handoff_package"),
                summary=result.get("summary"),
            )
        else:
            mark_job_failed(job_id, error=result.get("failure_reason", "Workflow failed"))
    except Exception as e:
        logger.exception("Planning workflow failed")
        mark_job_failed(job_id, error=str(e))


@app.post(
    "/run",
    response_model=PlanningRunResponse,
    summary="Start Planning",
    description="Start client-facing discovery and requirements workflow. Returns job_id; poll GET /status/{job_id}.",
)
def run_planning(request: PlanningRunRequest) -> PlanningRunResponse:
    """Start a Planning run: resolve the output workspace and dispatch the workflow.

    Preconditions:
        - ``request`` has passed Pydantic validation, so at least one of
          ``initial_brief`` / ``spec_content`` is non-blank.
    Postconditions:
        - A job is recorded and its workspace exists under
          ``AGENT_CACHE/planning``; returns the new ``job_id`` with status
          ``running``. Dispatch is via Temporal when enabled, else a background
          thread.
    Raises:
        - ``HTTPException(400)`` when the workspace cannot be resolved
          (``WorkspaceResolutionError``).
        - ``HTTPException(500)`` when job creation or Temporal dispatch fails (the
          job is marked failed and the workspace cleaned up first, respectively).
    """
    job_id = str(uuid.uuid4())
    # repo_path is an output folder, not a source to read: resolve any input
    # (empty, git URL, or client-side path) to a writable server-side directory.
    try:
        resolved_path = resolve_workspace(request.repo_path, request.client_name, job_id)
    except WorkspaceResolutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        create_job(job_id, resolved_path)
    except Exception as exc:
        # Job-store failures (DB connectivity, duplicate id) should surface as a
        # logged 500 with context rather than an opaque unhandled error. Remove
        # the just-created workspace so a failed insert leaves no orphan dir.
        shutil.rmtree(resolved_path, ignore_errors=True)
        logger.exception("Failed to create Planning job %s", job_id)
        raise HTTPException(status_code=500, detail=f"Failed to create job: {exc}") from exc

    try:
        from planning_team.temporal.client import is_temporal_enabled
        from planning_team.temporal.start_workflow import start_planning_workflow

        temporal_enabled = is_temporal_enabled()
    except ImportError:
        # Temporal extras not installed: fall through to thread mode.
        temporal_enabled = False

    if temporal_enabled:
        try:
            start_planning_workflow(
                job_id,
                resolved_path,
                request.client_name,
                request.initial_brief,
                request.spec_content,
                request.use_product_analysis,
                request.use_market_research,
            )
        except Exception as exc:
            # A dispatch failure (e.g. Temporal unreachable) must not leave the
            # job stuck "running": mark it failed and surface a logged 500.
            logger.exception("Failed to start Planning Temporal workflow %s", job_id)
            mark_job_failed(job_id, error=str(exc))
            raise HTTPException(status_code=500, detail=f"Failed to start workflow: {exc}") from exc
        return PlanningRunResponse(
            job_id=job_id,
            status="running",
            message="Planning started (Temporal). Poll GET /status/{job_id} for progress.",
        )

    # Thread mode (no Temporal): the background worker marks the job failed on
    # its own errors, so thread.start() is the only step left to run.
    thread = threading.Thread(
        target=_run_workflow_background,
        args=(
            job_id,
            resolved_path,
            request.client_name,
            request.initial_brief,
            request.spec_content,
            request.use_product_analysis,
            request.use_market_research,
        ),
        daemon=True,
    )
    thread.start()
    return PlanningRunResponse(
        job_id=job_id,
        status="running",
        message="Planning started. Poll GET /status/{job_id} for progress.",
    )


@app.get(
    "/status/{job_id}",
    response_model=PlanningStatusResponse,
    summary="Get job status",
)
def get_status(job_id: str) -> PlanningStatusResponse:
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return PlanningStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        repo_path=data.get("repo_path"),
        current_phase=data.get("current_phase"),
        status_text=data.get("status_text"),
        progress=data.get("progress", 0),
        pending_questions=data.get("pending_questions", []),
        waiting_for_answers=data.get("waiting_for_answers", False),
        error=data.get("error"),
        summary=data.get("summary"),
    )


@app.get(
    "/result/{job_id}",
    response_model=PlanningResultResponse,
    summary="Get job result",
)
def get_result(job_id: str) -> PlanningResultResponse:
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    success = data.get("status") == JOB_STATUS_COMPLETED
    handoff = data.get("handoff_package")
    return PlanningResultResponse(
        job_id=job_id,
        success=success,
        handoff_package=handoff,
        client_context_document_path=handoff.get("client_context_document_path")
        if isinstance(handoff, dict)
        else None,
        validated_spec_path=handoff.get("validated_spec_path")
        if isinstance(handoff, dict)
        else None,
        prd_path=handoff.get("prd_path") if isinstance(handoff, dict) else None,
        summary=data.get("summary"),
        failure_reason=data.get("error"),
    )


@app.get("/jobs", summary="List jobs")
def list_planning_jobs() -> dict:
    jobs = list_jobs(running_only=True)
    return {
        "jobs": [
            {
                "job_id": j.get("job_id"),
                "status": j.get("status"),
                "repo_path": j.get("repo_path"),
                "current_phase": j.get("current_phase"),
            }
            for j in jobs
        ]
    }


@app.post(
    "/{job_id}/answers",
    response_model=PlanningStatusResponse,
    summary="Submit answers to open questions",
)
def submit_answers(job_id: str, request: SubmitAnswersRequest) -> PlanningStatusResponse:
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not data.get("waiting_for_answers"):
        raise HTTPException(status_code=400, detail="Job is not waiting for answers")
    # Planning currently does not pause for answers mid-run; PRA is called with auto-answer callback.
    # This endpoint is for future use or when we add interactive question gates.
    update_job(job_id, waiting_for_answers=False)
    return get_status(job_id)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
