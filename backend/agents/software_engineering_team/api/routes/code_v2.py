"""SE team API — backend/frontend code-v2 sub-team run and status routes."""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from software_engineering_team.api.models import (
    BackendCodeV2RunRequest,
    BackendCodeV2RunResponse,
    BackendCodeV2StatusResponse,
    CodegenRunRequest,
    CodegenRunResponse,
    CodegenStatusResponse,
    FrontendCodeV2RunRequest,
    FrontendCodeV2RunResponse,
    FrontendCodeV2StatusResponse,
)
from software_engineering_team.shared.job_store import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    create_job,
    get_job,
    start_job_heartbeat_thread,
    update_job,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/frontend-code-v2/run",
    response_model=FrontendCodeV2RunResponse,
    summary="Run frontend-code-v2 agent team",
    description="Submit a task and repo path. Dispatches the frontend-code-v2 6-phase "
    "workflow to Temporal. Returns job_id immediately. Poll GET /frontend-code-v2/status/{job_id} "
    "for progress.",
)
def run_frontend_code_v2(request: FrontendCodeV2RunRequest) -> FrontendCodeV2RunResponse:
    """Start the frontend-code-v2 team on a task."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"repo_path does not exist or is not a directory: {request.repo_path}",
        )

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type="frontend_code_v2")

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow
        from software_engineering_team.temporal.constants import STANDALONE_TYPE_FRONTEND
        from software_engineering_team.temporal.start_workflow import start_standalone_workflow

        start_standalone_workflow(
            STANDALONE_TYPE_FRONTEND,
            job_id,
            request.repo_path,
            task_dict=request.task.model_dump(),
            architecture_overview=request.architecture or "",
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start frontend-code-v2 workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return FrontendCodeV2RunResponse(
        job_id=job_id,
        status="running",
        message="Frontend-code-v2 workflow started. Poll GET /frontend-code-v2/status/{job_id} for progress.",
    )


@router.get(
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
        status_text=data.get("status_text"),
    )


# ---------------------------------------------------------------------------
# Planning-V2
# ---------------------------------------------------------------------------


@router.post(
    "/backend-code-v2/run",
    response_model=BackendCodeV2RunResponse,
    summary="Run backend-code-v2 agent team",
    description="Submit a task and repo path. Dispatches the backend-code-v2 5-phase "
    "workflow to Temporal. Returns job_id immediately. Poll GET /backend-code-v2/status/{job_id} "
    "for progress.",
)
def run_backend_code_v2(request: BackendCodeV2RunRequest) -> BackendCodeV2RunResponse:
    """Start the backend-code-v2 team on a task."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"repo_path does not exist or is not a directory: {request.repo_path}",
        )

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type="backend_code_v2")

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow
        from software_engineering_team.temporal.constants import STANDALONE_TYPE_BACKEND
        from software_engineering_team.temporal.start_workflow import start_standalone_workflow

        start_standalone_workflow(
            STANDALONE_TYPE_BACKEND,
            job_id,
            request.repo_path,
            task_dict=request.task.model_dump(),
            architecture_overview=request.architecture or "",
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start backend-code-v2 workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return BackendCodeV2RunResponse(
        job_id=job_id,
        status="running",
        message="Backend-code-v2 workflow started. Poll GET /backend-code-v2/status/{job_id} for progress.",
    )


@router.get(
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
        status_text=data.get("status_text"),
    )


# ---------------------------------------------------------------------------
# Codegen (unified backend/frontend)
#
# A stepping-stone alongside the split /frontend-code-v2 and /backend-code-v2
# routes above (which stay unchanged for existing callers): one endpoint, an
# explicit `stack` field selects which codegen team stack runs. Internally
# this reuses the exact same Temporal dispatch those two routes already use
# (STANDALONE_TYPE_BACKEND/STANDALONE_TYPE_FRONTEND), just stack-selected at
# request time — no new Temporal workflow/activity code.
# ---------------------------------------------------------------------------


@router.post(
    "/code-v2/run",
    response_model=CodegenRunResponse,
    summary="Run codegen agent team (backend or frontend)",
    description="Submit a task, repo path, and stack ('backend' or 'frontend'). Dispatches the "
    "codegen team's 5-phase workflow to Temporal. Returns job_id immediately. Poll "
    "GET /code-v2/status/{job_id} for progress.",
)
def run_codegen(request: CodegenRunRequest) -> CodegenRunResponse:
    """Start the codegen team on a task for the requested stack."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"repo_path does not exist or is not a directory: {request.repo_path}",
        )

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type=f"{request.stack}_code_v2")
    update_job(job_id, stack=request.stack)

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow
        from software_engineering_team.temporal.constants import (
            STANDALONE_TYPE_BACKEND,
            STANDALONE_TYPE_FRONTEND,
        )
        from software_engineering_team.temporal.start_workflow import start_standalone_workflow

        standalone_type = (
            STANDALONE_TYPE_BACKEND if request.stack == "backend" else STANDALONE_TYPE_FRONTEND
        )
        start_standalone_workflow(
            standalone_type,
            job_id,
            request.repo_path,
            task_dict=request.task.model_dump(),
            architecture_overview=request.architecture or "",
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start codegen workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return CodegenRunResponse(
        job_id=job_id,
        status="running",
        message=(
            f"Codegen ({request.stack}) workflow started. "
            f"Poll GET /code-v2/status/{job_id} for progress."
        ),
    )


@router.get(
    "/code-v2/status/{job_id}",
    response_model=CodegenStatusResponse,
    summary="Get codegen job status",
    description="Returns what is done, what is in progress, and overall completion percentage.",
)
def get_codegen_status(job_id: str) -> CodegenStatusResponse:
    """Get the status of a codegen job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return CodegenStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        stack=data.get("stack"),
        repo_path=data.get("repo_path"),
        current_phase=data.get("current_phase"),
        current_microtask=data.get("current_microtask"),
        progress=data.get("progress", 0),
        microtasks_completed=data.get("microtasks_completed", 0),
        microtasks_total=data.get("microtasks_total", 0),
        completed_phases=data.get("completed_phases", []),
        error=data.get("error"),
        summary=data.get("summary"),
        status_text=data.get("status_text"),
    )


# ---------------------------------------------------------------------------
# Auto-Answer Endpoints
# ---------------------------------------------------------------------------
