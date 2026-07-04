"""FastAPI surface for the Job Matching team.

Endpoints (mounted by the unified API under ``/api/job-matching``):

* ``GET  /health``                       — liveness probe
* ``GET  /profile``                      — resolved job-seeker profile
* ``PUT  /profile``                      — save the profile as the career section
  of the central user profile
* ``POST /scan``                         — start an async scan, returns a job id
* ``GET  /scan/status/{job_id}``         — poll a scan; ``result`` holds the response
* ``GET  /scan/jobs``                    — list scan jobs
* ``POST /scan/jobs/{job_id}/cancel``    — cancel a pending/running scan
* ``DELETE /scan/jobs/{job_id}``         — delete a scan job record
* ``GET  /runs``                         — list persisted run summaries
* ``GET  /runs/{run_id}``                — a persisted run plus its ranked jobs
* ``GET  /listings``                     — aggregated listings (latest per fingerprint)
* ``PATCH /listings/{fingerprint}``      — set a listing's user status/notes
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict
from uuid import uuid4

from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware

from job_matching_team.models import (
    LISTING_FILTERS,
    JobMatchRequest,
    JobMatchResponse,
    Listing,
    ListingsResponse,
    ListingStateUpdate,
    RunDetail,
    RunSummary,
    ScanJobListItem,
    ScanJobListResponse,
    ScanJobResponse,
    ScanJobStatus,
)
from job_matching_team.postgres import SCHEMA as JOB_MATCHING_SCHEMA
from job_matching_team.profile.career_store import (
    CareerProfileUnavailableError,
    save_career_profile,
)
from job_matching_team.profile.loader import load_job_seeker_profile
from job_matching_team.profile.model import JobSeekerProfile
from job_matching_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    cancel_job,
    create_job,
    delete_job,
    get_job,
    is_job_cancelled,
    list_jobs,
    update_job,
)
from shared_app import create_team_app

logger = logging.getLogger(__name__)


def _register_user_profile_schema() -> None:
    """Ensure the central user-profile tables exist for career-profile writes.

    The unified API registers this schema too, but this service runs in its own
    container and must not depend on unified-API startup ordering. Registration
    is idempotent DDL and defensive (logged, never raised into startup).
    """
    try:
        from shared_postgres import register_team_schemas
        from user_profile import SCHEMA as USER_PROFILE_SCHEMA

        register_team_schemas(USER_PROFILE_SCHEMA)
    except Exception:  # noqa: BLE001 - startup must not fail on optional DDL
        logger.warning("Could not register user_profile schema at startup", exc_info=True)


app = create_team_app(
    service_name="job-matching",
    team_key="job_matching",
    title="Job Matching API",
    description="Scans open roles matching a job-seeker profile and ranks the best to apply for",
    version="1.0.0",
    postgres_schema=JOB_MATCHING_SCHEMA,
    on_startup=_register_user_profile_schema,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_orchestrator():  # noqa: ANN202 - lazy to keep import cheap / mockable in tests
    from job_matching_team.orchestrator import JobMatchingOrchestrator

    return JobMatchingOrchestrator()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/profile", response_model=JobSeekerProfile)
def get_profile() -> JobSeekerProfile:
    """Return the resolved standing job-seeker profile."""
    return load_job_seeker_profile()


@app.put("/profile", response_model=JobSeekerProfile)
def put_profile(payload: JobSeekerProfile) -> JobSeekerProfile:
    """Save the profile as the career section of the central user profile.

    Postconditions:
        * On success the saved profile is returned and subsequent
          ``GET /profile`` calls resolve to it (career section wins).
    """
    try:
        return save_career_profile(payload)
    except CareerProfileUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/listings", response_model=ListingsResponse)
def list_listings(status: str = "active", limit: int = 200) -> ListingsResponse:
    """Return aggregated listings (latest snapshot per fingerprint) plus counts."""
    if status not in LISTING_FILTERS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status filter {status!r}; expected one of {sorted(LISTING_FILTERS)}",
        )
    if limit < 1:
        raise HTTPException(status_code=422, detail="limit must be a positive integer")
    from job_matching_team.store import get_store

    return get_store().list_listings(status=status, limit=limit)


@app.patch("/listings/{fingerprint}", response_model=Listing)
def update_listing(fingerprint: str, payload: ListingStateUpdate) -> Listing:
    """Set a listing's user status (and optionally notes), returning the fresh listing."""
    from job_matching_team.store import get_store

    listing = get_store().update_listing_state(fingerprint, payload)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return listing


def _run_scan_background(job_id: str, request: JobMatchRequest) -> None:
    try:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = _get_orchestrator().run(request, job_id=job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump(mode="json"))
    except Exception as exc:
        logger.exception("Job matching scan %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))


def _dispatch_scan_via_temporal(job_id: str, payload: JobMatchRequest) -> bool:
    """Dispatch a scan to Temporal when enabled. Returns True if dispatched.

    Falls back to the daemon-thread path (returns False) when Temporal is
    disabled or the Temporal package can't be imported, so behavior is
    unchanged whenever ``TEMPORAL_ADDRESS`` is unset.
    """
    try:
        from shared_temporal import is_temporal_enabled

        if not is_temporal_enabled():
            return False
        from job_matching_team.temporal.start_workflow import start_job_matching_workflow
    except ImportError:
        return False
    start_job_matching_workflow(job_id, payload.model_dump(mode="json"))
    logger.info("Job matching scan %s dispatched via Temporal", job_id)
    return True


@app.post("/scan", response_model=ScanJobResponse)
def start_scan(payload: JobMatchRequest) -> ScanJobResponse:
    """Start an async scan. Poll ``GET /scan/status/{job_id}`` for the result.

    Runs the scan on a durable Temporal workflow when ``TEMPORAL_ADDRESS`` is
    set, else on a daemon thread. Both paths track status through the same job
    store, so ``GET /scan/status/{job_id}`` is identical either way.
    """
    job_id = str(uuid4())
    create_job(job_id)
    if not _dispatch_scan_via_temporal(job_id, payload):
        thread = threading.Thread(target=_run_scan_background, args=(job_id, payload), daemon=True)
        thread.start()
    return ScanJobResponse(job_id=job_id, status=JOB_STATUS_PENDING)


@app.get("/scan/status/{job_id}", response_model=ScanJobStatus)
def get_scan_status(job_id: str) -> ScanJobStatus:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    raw_result = data.get("result")
    return ScanJobStatus(
        job_id=data.get("job_id", job_id),
        status=data.get("status", JOB_STATUS_PENDING),
        result=JobMatchResponse.model_validate(raw_result) if raw_result else None,
        error=data.get("error"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@app.get("/scan/jobs", response_model=ScanJobListResponse)
def list_scan_jobs(running_only: bool = False) -> ScanJobListResponse:
    statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    items = [
        ScanJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            created_at=j.get("created_at"),
            updated_at=j.get("updated_at"),
        )
        for j in list_jobs(statuses=statuses)
    ]
    return ScanJobListResponse(jobs=items)


@app.post("/scan/jobs/{job_id}/cancel")
def cancel_scan_job(job_id: str) -> Dict[str, Any]:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if cancel_job(job_id):
        return {"job_id": job_id, "status": JOB_STATUS_CANCELLED, "success": True}
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "success": False,
        "message": f"Cannot cancel job in status {data.get('status')}",
    }


@app.delete("/scan/jobs/{job_id}")
def delete_scan_job(job_id: str) -> Dict[str, Any]:
    if get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "deleted": True}


@app.get("/runs", response_model=list[RunSummary])
def list_runs(limit: int = 50) -> list[RunSummary]:
    """List persisted run summaries, newest first."""
    from job_matching_team.store import get_store

    return get_store().list_runs(limit=limit)


@app.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str) -> RunDetail:
    """Return a persisted run plus its ranked jobs."""
    from job_matching_team.store import get_store

    detail = get_store().get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return detail
