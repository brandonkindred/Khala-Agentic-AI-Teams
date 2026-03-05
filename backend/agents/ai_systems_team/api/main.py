"""
FastAPI endpoints for the AI Systems Team.

Provides REST API for AI system blueprint generation and job tracking.
"""

import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

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
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    add_completed_phase,
    create_job,
    get_job,
    list_jobs,
    mark_job_completed,
    mark_job_failed,
    mark_job_running,
    update_job,
)

app = FastAPI(
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
) -> None:
    """Background thread function for running AI system generation workflow."""
    try:
        mark_job_running(job_id)
        
        def job_updater(
            current_phase: Optional[str] = None,
            progress: Optional[int] = None,
            status_text: Optional[str] = None,
        ) -> None:
            """Callback to update job status during workflow execution."""
            updates: Dict[str, Any] = {}
            
            if current_phase is not None:
                updates["current_phase"] = current_phase
            if progress is not None:
                updates["progress"] = progress
            if status_text is not None:
                updates["status_text"] = status_text
            
            if updates:
                update_job(job_id, **updates)
        
        blueprint = orchestrator.run_workflow(
            project_name=project_name,
            spec_path=spec_path,
            constraints=constraints,
            output_dir=output_dir,
            job_updater=job_updater,
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
    
    blueprint = None
    if data.get("status") == JOB_STATUS_COMPLETED and data.get("blueprint"):
        blueprint = AgentBlueprint(**data["blueprint"])
    
    return AISystemStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        project_name=data.get("project_name"),
        current_phase=data.get("current_phase"),
        progress=data.get("progress", 0),
        completed_phases=data.get("completed_phases", []),
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


@app.get(
    "/blueprints",
    summary="List generated blueprints",
    description="List all generated AI system blueprints (in-memory).",
)
def list_blueprints() -> Dict[str, List[str]]:
    """List all generated blueprint project names."""
    return {"blueprints": orchestrator.list_blueprints()}


@app.get(
    "/blueprints/{project_name}",
    response_model=AgentBlueprint,
    summary="Get blueprint by project name",
    description="Get a previously generated blueprint by project name.",
)
def get_blueprint(project_name: str) -> AgentBlueprint:
    """Get a blueprint by project name."""
    blueprint = orchestrator.get_blueprint(project_name)
    
    if not blueprint:
        raise HTTPException(status_code=404, detail=f"Blueprint '{project_name}' not found")
    
    return blueprint


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
