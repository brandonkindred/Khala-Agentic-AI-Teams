"""
FastAPI endpoints for the AI Systems Team.

Provides REST API for AI system blueprint generation and job tracking.
"""

import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from job_service_client import RESTARTABLE_STATUSES, RESUMABLE_STATUSES, validate_job_for_action
from shared.app import create_team_app  # noqa: E402

from ..models import (
    AgentBlueprint,
    AISystemJobResponse,
    AISystemJobsListResponse,
    AISystemJobSummary,
    AISystemRequest,
    AISystemStatusResponse,
)
from ..orchestrator import AISystemsOrchestrator
from ..shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    list_jobs,
    make_job_updater,
    mark_job_completed,
    mark_job_failed,
    mark_job_running,
    update_job,
)
from ..shared.job_store import (
    cancel_job as store_cancel_job,
)
from ..shared.job_store import (
    delete_job as store_delete_job,
)
from ..shared.job_store import (
    reset_job as store_reset_job,
)

app = create_team_app(
    service_name="ai-systems-team",
    team_key="ai_systems",
    title="AI Systems API",
    description="API for generating AI agent system blueprints from specifications",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = AISystemsOrchestrator()


def _run_build_background(
    job_id: str,
    project_name: str,
    spec_path: str,
    constraints: Dict[str, Any],
    output_dir: Optional[str],
    resume_blueprint: Optional[Any] = None,
) -> None:
    """Background thread function for running AI system generation workflow.

    Thread-mode counterpart to ``AISystemsBuildWorkflow``: it shares the same
    ``make_job_updater`` progress callback so both runtimes write identical
    job-store fields. The Temporal path runs each phase as its own activity
    instead (see ``temporal/workflows.py``).
    """
    try:
        mark_job_running(job_id)

        job_updater = make_job_updater(job_id)

        blueprint = orchestrator.run_workflow(
            project_name=project_name,
            spec_path=spec_path,
            constraints=constraints,
            output_dir=output_dir,
            job_updater=job_updater,
            resume_blueprint=resume_blueprint,
        )

        if blueprint.success:
            mark_job_completed(job_id, blueprint=blueprint.model_dump())
        else:
            mark_job_failed(job_id, error=blueprint.error or "Build failed")

    except Exception as e:
        mark_job_failed(job_id, error=str(e))


@app.post(
    "/build",
    response_model=AISystemJobResponse,
    summary="Start AI system build job",
    description="Start an asynchronous AI system generation job. "
    "Returns a job_id to poll for status.",
)
def start_build(request: AISystemRequest) -> AISystemJobResponse:
    """Start a new AI system build job."""
    job_id = str(uuid.uuid4())

    create_job(
        job_id=job_id,
        project_name=request.project_name,
        spec_path=request.spec_path,
        constraints=request.constraints,
        output_dir=request.output_dir,
    )

    try:
        from ai_systems_team.temporal.client import is_temporal_enabled
        from ai_systems_team.temporal.start_workflow import start_build_workflow

        if is_temporal_enabled():
            start_build_workflow(
                job_id,
                request.project_name,
                request.spec_path,
                request.constraints,
                request.output_dir,
            )
            return AISystemJobResponse(
                job_id=job_id,
                status=JOB_STATUS_RUNNING,
                message="Build started (Temporal). Poll GET /build/status/{job_id} for progress.",
            )
    except ImportError:
        pass

    thread = threading.Thread(
        target=_run_build_background,
        args=(
            job_id,
            request.project_name,
            request.spec_path,
            request.constraints,
            request.output_dir,
        ),
        daemon=True,
    )
    thread.start()

    return AISystemJobResponse(
        job_id=job_id,
        status=JOB_STATUS_RUNNING,
        message="Build started. Poll GET /build/status/{job_id} for progress.",
    )


@app.get(
    "/build/status/{job_id}",
    response_model=AISystemStatusResponse,
    summary="Get build job status",
    description="Get the current status of an AI system build job including phase progress.",
)
def get_build_status(job_id: str) -> AISystemStatusResponse:
    """Get status of an AI system build job."""
    data = get_job(job_id)

    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # The checkpointed blueprint snapshot is the single source of truth for phase
    # completion — both the thread-mode orchestrator and the Temporal per-phase
    # activities maintain ``blueprint.completed_phases`` (checkpointed after each
    # phase), so read live progress straight from it.
    stored_bp = data.get("blueprint")

    blueprint = None
    if data.get("status") == JOB_STATUS_COMPLETED and stored_bp:
        blueprint = AgentBlueprint(**stored_bp)

    completed_phases = stored_bp.get("completed_phases", []) if isinstance(stored_bp, dict) else []

    return AISystemStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        project_name=data.get("project_name"),
        current_phase=data.get("current_phase"),
        progress=data.get("progress", 0),
        completed_phases=completed_phases,
        error=data.get("error"),
        blueprint=blueprint,
    )


@app.get(
    "/build/jobs",
    response_model=AISystemJobsListResponse,
    summary="List build jobs",
    description="List all AI system build jobs, optionally filtered to running only.",
)
def list_build_jobs(
    running_only: bool = Query(False, description="Filter to running/pending jobs only"),
) -> AISystemJobsListResponse:
    """List all AI system build jobs."""
    jobs_data = list_jobs(running_only=running_only)

    jobs = [
        AISystemJobSummary(
            job_id=j["job_id"],
            project_name=j.get("project_name", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            created_at=j.get("created_at"),
            current_phase=j.get("current_phase"),
            progress=j.get("progress", 0),
        )
        for j in jobs_data
    ]

    return AISystemJobsListResponse(jobs=jobs)


class CancelBuildJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteBuildJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


@app.post(
    "/build/job/{job_id}/cancel",
    response_model=CancelBuildJobResponse,
    summary="Cancel a build job",
    description="Set job status to cancelled. Only allowed for pending or running jobs.",
)
def cancel_build_job(job_id: str) -> CancelBuildJobResponse:
    """Cancel a pending or running build job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current = data.get("status", JOB_STATUS_PENDING)
    if current not in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING):
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {current}. Cannot cancel.",
        )
    store_cancel_job(job_id)
    return CancelBuildJobResponse(job_id=job_id, message="Job cancellation requested.")


@app.delete(
    "/build/job/{job_id}",
    response_model=DeleteBuildJobResponse,
    summary="Delete a build job",
    description="Remove the job from the store. Returns 404 if not found.",
)
def delete_build_job(job_id: str) -> DeleteBuildJobResponse:
    """Delete a build job by id."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not store_delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteBuildJobResponse(job_id=job_id, message="Job deleted.")


@app.post(
    "/build/job/{job_id}/resume",
    response_model=AISystemJobResponse,
    summary="Resume an interrupted build job",
    description="Re-enter the build pipeline at the last completed phase. Skips phases that already succeeded.",
)
def resume_build_job(job_id: str) -> AISystemJobResponse:
    """Resume a build job from its last checkpoint."""
    try:
        data = validate_job_for_action(get_job(job_id), job_id, RESUMABLE_STATUSES, "resumed")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    project_name = data.get("project_name")
    spec_path = data.get("spec_path")
    if not project_name or not spec_path:
        raise HTTPException(status_code=400, detail="Job is missing project_name or spec_path.")

    # Reconstruct partial blueprint from stored data
    resume_bp = None
    stored_bp = data.get("blueprint")
    if stored_bp and isinstance(stored_bp, dict):
        try:
            resume_bp = AgentBlueprint(**stored_bp)
        except Exception:
            pass  # corrupt data — will re-run all phases

    update_job(job_id, status=JOB_STATUS_RUNNING, error=None)

    try:
        from ai_systems_team.temporal.client import is_temporal_enabled
        from ai_systems_team.temporal.start_workflow import start_build_workflow

        if is_temporal_enabled():
            # The workflow's ``begin`` activity reads the checkpointed blueprint
            # (completed_phases + per-phase results) straight from the job store and
            # skips the phases already done, so no separate resume payload is needed.
            start_build_workflow(
                job_id, project_name, spec_path, data.get("constraints", {}), data.get("output_dir")
            )
            return AISystemJobResponse(
                job_id=job_id, status="running", message="Job resumed (Temporal)."
            )
    except ImportError:
        pass

    thread = threading.Thread(
        target=_run_build_background,
        args=(job_id, project_name, spec_path, data.get("constraints", {}), data.get("output_dir")),
        kwargs={"resume_blueprint": resume_bp},
        daemon=True,
    )
    thread.start()

    return AISystemJobResponse(
        job_id=job_id, status="running", message="Job resumed. Skipping completed phases."
    )


@app.post(
    "/build/job/{job_id}/restart",
    response_model=AISystemJobResponse,
    summary="Restart a build job from scratch",
    description="Reset the job to initial state and re-run the full pipeline with the same inputs.",
)
def restart_build_job(job_id: str) -> AISystemJobResponse:
    """Restart a build job from the beginning."""
    try:
        data = validate_job_for_action(get_job(job_id), job_id, RESTARTABLE_STATUSES, "restarted")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    project_name = data.get("project_name")
    spec_path = data.get("spec_path")
    if not project_name or not spec_path:
        raise HTTPException(status_code=400, detail="Job is missing project_name or spec_path.")

    store_reset_job(job_id)

    try:
        from ai_systems_team.temporal.client import is_temporal_enabled
        from ai_systems_team.temporal.start_workflow import start_build_workflow

        if is_temporal_enabled():
            start_build_workflow(
                job_id, project_name, spec_path, data.get("constraints", {}), data.get("output_dir")
            )
            return AISystemJobResponse(
                job_id=job_id, status="running", message="Job restarted (Temporal)."
            )
    except ImportError:
        pass

    thread = threading.Thread(
        target=_run_build_background,
        args=(job_id, project_name, spec_path, data.get("constraints", {}), data.get("output_dir")),
        daemon=True,
    )
    thread.start()

    return AISystemJobResponse(
        job_id=job_id, status="running", message="Job restarted from scratch."
    )


@app.get(
    "/blueprints",
    summary="List generated blueprints",
    description="List generated AI system blueprints (in-memory cache plus completed jobs).",
)
def list_blueprints() -> Dict[str, List[str]]:
    """List all generated blueprint project names.

    Unions the in-memory orchestrator cache (populated by thread-mode runs) with
    completed jobs in the durable job store, so Temporal builds — which complete via
    the per-phase workflow without touching the in-memory cache — are also listed.
    """
    names = set(orchestrator.list_blueprints())
    for job in list_jobs():
        if (
            job.get("status") == JOB_STATUS_COMPLETED
            and job.get("blueprint")
            and job.get("project_name")
        ):
            names.add(job["project_name"])
    return {"blueprints": sorted(names)}


@app.get(
    "/blueprints/{project_name}",
    response_model=AgentBlueprint,
    summary="Get blueprint by project name",
    description="Get a previously generated blueprint by project name.",
)
def get_blueprint(project_name: str) -> AgentBlueprint:
    """Get a blueprint by project name.

    Prefers the in-memory orchestrator cache, then falls back to the durable job
    store: Temporal builds finish in the per-phase workflow without populating the
    cache, so the completed blueprint only lives on the job record (which also
    survives a restart). The most recent completed job for the name wins.
    """
    blueprint = orchestrator.get_blueprint(project_name)
    if blueprint:
        return blueprint

    match = None
    for job in list_jobs():
        if (
            job.get("project_name") == project_name
            and job.get("status") == JOB_STATUS_COMPLETED
            and job.get("blueprint")
        ):
            match = job
    if match is not None:
        return AgentBlueprint(**match["blueprint"])

    raise HTTPException(status_code=404, detail=f"Blueprint '{project_name}' not found")


@app.get("/health", summary="Health check")
def health_check() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "ai-systems"}


@app.get("/", summary="API info")
def api_info() -> Dict[str, str]:
    """API information endpoint."""
    return {
        "service": "AI Systems API",
        "version": "1.0.0",
        "description": "Spec-driven AI agent system factory",
        "docs": "/docs",
    }
