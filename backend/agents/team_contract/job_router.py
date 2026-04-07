"""
Standard job lifecycle router for team services.

Provides a reusable FastAPI APIRouter with standard CRUD endpoints for job
management, backed by the shared ``job_service_client``. Teams include this
router to get consistent job endpoints without reimplementing them.

Usage::

    from team_contract.job_router import create_job_router

    router = create_job_router("blogging")
    app.include_router(router)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class JobCreateRequest(BaseModel):
    """Standard request to create a new job."""

    job_id: Optional[str] = None  # Auto-generated if not provided
    metadata: Dict[str, Any] = {}


class JobStatusResponse(BaseModel):
    """Standard job status response."""

    job_id: str
    team: str
    status: str
    progress: Optional[int] = None
    status_text: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def create_job_router(team: str) -> APIRouter:
    """Create a standard job lifecycle router for a team.

    Endpoints:
        POST /jobs           — Create a new job
        GET  /jobs           — List jobs (with optional status filter)
        GET  /jobs/{job_id}  — Get job status
        DELETE /jobs/{job_id} — Cancel a job
    """
    router = APIRouter(prefix="/jobs", tags=[f"{team}-jobs"])

    def _get_manager():
        """Lazy import to avoid circular deps at module load time."""
        from job_service_client import JobServiceClient

        return JobServiceClient(team=team)

    @router.post("/", status_code=201)
    def create_job(request: JobCreateRequest) -> Dict[str, Any]:
        """Create a new job for this team."""
        import uuid

        manager = _get_manager()
        job_id = request.job_id or str(uuid.uuid4())
        manager.create_job(job_id, status="pending", **request.metadata)
        return {"job_id": job_id, "team": team, "status": "pending"}

    @router.get("/")
    def list_jobs(
        status: Optional[str] = Query(None, description="Filter by status"),
    ) -> List[Dict[str, Any]]:
        """List all jobs for this team."""
        manager = _get_manager()
        jobs = manager.list_jobs()
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        return jobs

    @router.get("/{job_id}")
    def get_job(job_id: str) -> Dict[str, Any]:
        """Get the status of a specific job."""
        manager = _get_manager()
        job = manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return job

    @router.delete("/{job_id}")
    def cancel_job(job_id: str) -> Dict[str, Any]:
        """Cancel a running job."""
        manager = _get_manager()
        job = manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        manager.update_job(job_id, status="cancelled")
        return {"job_id": job_id, "cancelled": True}

    class _ResumeRequest(BaseModel):
        key: Optional[str] = None
        value: Any = None

    @router.post("/{job_id}/resume")
    def resume_job(job_id: str, request: _ResumeRequest) -> Dict[str, Any]:
        """Resume a paused job by submitting input for a ``waiting_for`` key.

        Works uniformly for Temporal and thread-mode jobs because both
        record pauses via :func:`shared_temporal.wait_for_input`.
        """
        manager = _get_manager()
        job = manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        waiting = job.get("waiting_for") or {}
        key = request.key
        if key is None:
            if len(waiting) == 1:
                key = next(iter(waiting))
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Job is not waiting for a single input; specify 'key'. "
                        f"waiting_for={list(waiting)}"
                    ),
                )
        if key not in waiting:
            raise HTTPException(
                status_code=409, detail=f"Job is not waiting for key '{key}'"
            )

        from shared_temporal.checkpoints import submit_input

        submit_input(team, job_id, key, request.value)

        # If running under Temporal, also signal the workflow so
        # ``workflow.wait_condition`` handlers wake up immediately.
        try:
            from shared_temporal.client import (
                get_temporal_client,
                get_temporal_loop,
                is_temporal_enabled,
            )

            if is_temporal_enabled():
                client = get_temporal_client()
                loop = get_temporal_loop()
                if client is not None and loop is not None:
                    import asyncio

                    async def _signal() -> None:
                        handle = client.get_workflow_handle(f"{team}-{job_id}")
                        await handle.signal("submit_input", {"key": key, "value": request.value})

                    asyncio.run_coroutine_threadsafe(_signal(), loop)
        except Exception:  # pragma: no cover - signal is best-effort
            logger.exception("Resume signal failed for %s/%s", team, job_id)

        return {"job_id": job_id, "resumed": True, "key": key}

    return router


# Convenience: pre-built router for teams that don't need customization.
# Teams that need custom job logic should use create_job_router() instead.
job_router = create_job_router
