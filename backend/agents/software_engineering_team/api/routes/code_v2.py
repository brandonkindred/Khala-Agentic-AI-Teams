"""SE team API — backend/frontend code-v2 sub-team run and status routes.

Route handlers register on a module-local ``APIRouter`` that ``main`` mounts with
``app.include_router``; absolute paths are unchanged. Monkeypatched collaborators
(background runners, ``SUPERVISOR_LOG_DIR``) are dereferenced through the ``main``
module object at call time so ``monkeypatch.setattr(main, ...)`` still takes effect.
"""

import logging
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from software_engineering_team.api import main as _main
from software_engineering_team.api.models import (
    BackendCodeV2RunRequest,
    BackendCodeV2RunResponse,
    BackendCodeV2StatusResponse,
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
    description="Submit a task and repo path. Starts the frontend-code-v2 6-phase workflow in the background. "
    "Returns job_id immediately. Poll GET /frontend-code-v2/status/{job_id} for progress.",
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

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow or frontend-code-v2 thread
        from software_engineering_team.temporal.client import is_temporal_enabled
        from software_engineering_team.temporal.constants import STANDALONE_TYPE_FRONTEND
        from software_engineering_team.temporal.start_workflow import start_standalone_workflow

        if is_temporal_enabled():
            start_standalone_workflow(
                STANDALONE_TYPE_FRONTEND,
                job_id,
                request.repo_path,
                task_dict=request.task.model_dump(),
                architecture_overview=request.architecture or "",
            )
        else:
            thread = threading.Thread(
                target=_main._run_frontend_code_v2_background,
                args=(
                    job_id,
                    request.repo_path,
                    request.task.model_dump(),
                    request.architecture or "",
                ),
            )
            thread.daemon = True
            thread.start()
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
    description="Submit a task and repo path. Starts the backend-code-v2 5-phase workflow in the background. "
    "Returns job_id immediately. Poll GET /backend-code-v2/status/{job_id} for progress.",
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

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow or backend-code-v2 thread
        from software_engineering_team.temporal.client import is_temporal_enabled
        from software_engineering_team.temporal.constants import STANDALONE_TYPE_BACKEND
        from software_engineering_team.temporal.start_workflow import start_standalone_workflow

        if is_temporal_enabled():
            start_standalone_workflow(
                STANDALONE_TYPE_BACKEND,
                job_id,
                request.repo_path,
                task_dict=request.task.model_dump(),
                architecture_overview=request.architecture or "",
            )
        else:
            thread = threading.Thread(
                target=_main._run_backend_code_v2_background,
                args=(
                    job_id,
                    request.repo_path,
                    request.task.model_dump(),
                    request.architecture or "",
                ),
            )
            thread.daemon = True
            thread.start()
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
# Auto-Answer Endpoints
# ---------------------------------------------------------------------------
