"""Job Service — a standalone FastAPI microservice for centralized job management.

Persists job data in a dedicated Postgres database (``strands_jobs``).  All
agent teams interact with this service over HTTP via the ``JobServiceClient``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from db import (
    append_event as db_append_event,
)
from db import (
    apply_patch as db_apply_patch,
)
from db import (
    close_pool as db_close_pool,
)
from db import (
    create_job as db_create_job,
)
from db import (
    delete_job as db_delete_job,
)
from db import (
    get_job as db_get_job,
)
from db import (
    heartbeat as db_heartbeat,
)
from db import (
    list_jobs as db_list_jobs,
)
from db import (
    mark_all_active_jobs_failed as db_mark_all_active_jobs_failed,
)
from db import (
    mark_all_active_jobs_interrupted as db_mark_all_active_jobs_interrupted,
)
from db import (
    mark_stale_active_jobs_failed as db_mark_stale_active_jobs_failed,
)
from db import (
    replace_job as db_replace_job,
)
from db import (
    update_job as db_update_job,
)
from fastapi import FastAPI, HTTPException, Query
from models import (
    AppendEventRequest,
    ApplyPatchRequest,
    CreateJobRequest,
    DeleteResponse,
    HealthResponse,
    JobListResponse,
    JobResponse,
    MarkAllFailedRequest,
    MarkInterruptedResponse,
    MarkStaleRequest,
    MarkStaleResponse,
    ReplaceJobRequest,
    UpdateJobRequest,
)
from postgres import SCHEMA as JOB_SERVICE_SCHEMA

from shared_postgres import close_pool as shared_close_pool
from shared_postgres import register_team_schemas

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("job_service")


@asynccontextmanager
async def lifespan(application: FastAPI):
    register_team_schemas(JOB_SERVICE_SCHEMA)
    logger.info("Job service started")
    yield
    db_close_pool()
    shared_close_pool()
    logger.info("Job service stopped")


app = FastAPI(title="Strands Job Service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@app.post("/jobs/{team}", response_model=JobResponse)
def create_job(team: str, req: CreateJobRequest):
    db_create_job(team, req.job_id, status=req.status, **req.fields)
    return JobResponse(job=db_get_job(team, req.job_id))


@app.post("/jobs/{team}/{job_id}/replace", response_model=JobResponse)
def replace_job(team: str, job_id: str, req: ReplaceJobRequest):
    db_replace_job(team, job_id, req.payload)
    return JobResponse(job=db_get_job(team, job_id))


@app.get("/jobs/{team}/{job_id}", response_model=JobResponse)
def get_job(team: str, job_id: str):
    job = db_get_job(team, job_id)
    return JobResponse(job=job)


@app.delete("/jobs/{team}/{job_id}", response_model=DeleteResponse)
def delete_job(team: str, job_id: str):
    deleted = db_delete_job(team, job_id)
    return DeleteResponse(deleted=deleted)


@app.get("/jobs/{team}", response_model=JobListResponse)
def list_jobs(team: str, statuses: list[str] | None = Query(default=None)):  # noqa: B008
    jobs = db_list_jobs(team, statuses=statuses)
    return JobListResponse(jobs=jobs)


@app.patch("/jobs/{team}/{job_id}", response_model=JobResponse)
def update_job(team: str, job_id: str, req: UpdateJobRequest):
    db_update_job(team, job_id, heartbeat=req.heartbeat, **req.fields)
    return JobResponse(job=db_get_job(team, job_id))


# ---------------------------------------------------------------------------
# Atomic operations
# ---------------------------------------------------------------------------


@app.post("/jobs/{team}/{job_id}/apply", response_model=JobResponse)
def apply_patch(team: str, job_id: str, req: ApplyPatchRequest):
    db_apply_patch(
        team,
        job_id,
        merge_fields=req.merge_fields,
        merge_nested=req.merge_nested,
        append_to=req.append_to,
        increment=req.increment,
    )
    return JobResponse(job=db_get_job(team, job_id))


@app.post("/jobs/{team}/{job_id}/event", response_model=JobResponse)
def append_event(team: str, job_id: str, req: AppendEventRequest):
    db_append_event(
        team,
        job_id,
        action=req.action,
        outcome=req.outcome,
        details=req.details,
        status=req.status,
    )
    return JobResponse(job=db_get_job(team, job_id))


@app.post("/jobs/{team}/{job_id}/heartbeat")
def heartbeat(team: str, job_id: str):
    found = db_heartbeat(team, job_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found for team {team}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Bulk / lifecycle
# ---------------------------------------------------------------------------


@app.post("/jobs/{team}/mark-stale-failed", response_model=MarkStaleResponse)
def mark_stale_failed(team: str, req: MarkStaleRequest):
    failed_ids = db_mark_stale_active_jobs_failed(
        team,
        stale_after_seconds=req.stale_after_seconds,
        reason=req.reason,
        waiting_field=req.waiting_field,
    )
    return MarkStaleResponse(failed_job_ids=failed_ids)


@app.post("/jobs/{team}/mark-all-running-failed", response_model=MarkStaleResponse)
def mark_all_running_failed(team: str, req: MarkAllFailedRequest):
    failed_ids = db_mark_all_active_jobs_failed(team, req.reason)
    return MarkStaleResponse(failed_job_ids=failed_ids)


@app.post("/jobs/{team}/mark-all-running-interrupted", response_model=MarkInterruptedResponse)
def mark_all_running_interrupted(team: str, req: MarkAllFailedRequest):
    """Mark all active jobs as interrupted (service shutdown). Uses same request body as failed."""
    ids = db_mark_all_active_jobs_interrupted(team, req.reason)
    return MarkInterruptedResponse(interrupted_job_ids=ids)
