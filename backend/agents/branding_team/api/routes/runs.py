"""Branding API — brand run submission and job status / cancel / delete endpoints."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from branding_team.api import background as _bg
from branding_team.api.models import (
    BrandJobListItem,
    BrandJobListResponse,
    BrandJobStatusResponse,
    RunBrandJobResponse,
    RunBrandRequest,
)
from branding_team.api.state import _parse_target_phase
from branding_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    cancel_job,
    delete_job,
    get_job,
    list_jobs,
)

router = APIRouter()


@router.post(
    "/clients/{client_id}/brands/{brand_id}/run",
    response_model=RunBrandJobResponse,
    response_model_exclude_none=True,
)
def run_brand(client_id: str, brand_id: str, payload: RunBrandRequest) -> RunBrandJobResponse:
    """Submit a branding run job. Poll GET /branding/status/{job_id} for results.

    Preconditions:
        ``client_id`` and ``brand_id`` are non-empty path strings; ``payload`` is
        a validated ``RunBrandRequest``. ``payload.target_phase`` is either unset
        or a recognized phase name (an unrecognized value parses to ``None``,
        meaning "run all phases").
    Postconditions:
        Returns a ``RunBrandJobResponse`` carrying the id of the async job just
        enqueued; the run itself proceeds in the background.
    """
    target_phase = _parse_target_phase(payload.target_phase)
    return _bg._submit_brand_run(client_id, brand_id, payload, target_phase)


@router.post(
    "/clients/{client_id}/brands/{brand_id}/run/{phase}",
    response_model=RunBrandJobResponse,
    response_model_exclude_none=True,
)
def run_brand_phase(
    client_id: str, brand_id: str, phase: str, payload: RunBrandRequest
) -> RunBrandJobResponse:
    """Submit a branding run job scoped to a specific phase.

    Preconditions:
        ``client_id`` and ``brand_id`` are non-empty path strings; ``payload`` is
        a validated ``RunBrandRequest``. ``phase`` is a path segment naming the
        target phase.
    Postconditions:
        Returns a ``RunBrandJobResponse`` for the async job scoped to ``phase``.
        Raises 400 "Invalid phase: …" when ``phase`` does not parse to a known
        phase (unlike ``run_brand``, an unparseable value here is rejected rather
        than treated as "run all").
    """
    target_phase = _parse_target_phase(phase)
    if target_phase is None:
        raise HTTPException(status_code=400, detail=f"Invalid phase: {phase}")
    return _bg._submit_brand_run(client_id, brand_id, payload, target_phase)


@router.get("/branding/status/{job_id}", response_model=BrandJobStatusResponse)
def get_branding_job_status(job_id: str) -> BrandJobStatusResponse:
    """Return the current status/result payload for a branding run job.

    Preconditions:
        ``job_id`` is a non-empty path string.
    Postconditions:
        Returns the job's ``BrandJobStatusResponse``, validated from the raw job
        store payload (extra keys ignored, new response fields picked up
        automatically). ``job_id`` and ``status`` are defaulted explicitly before
        validation because they are required and the store may not set them.
        Raises 404 "Job not found" when no job matches ``job_id``.
    """
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # model_validate picks up any response field the job store carries (extra
    # keys are ignored — pydantic's default), so a new BrandJobStatusResponse
    # field is populated automatically instead of needing a matching
    # data.get(...) added here. job_id/status are computed explicitly first:
    # both are required (non-Optional) fields, and the job store doesn't always
    # set them, so model_validate(data) alone would raise on a missing key
    # where the old hand-mapping defaulted instead.
    return BrandJobStatusResponse.model_validate(
        {
            **data,
            "job_id": data.get("job_id", job_id),
            "status": data.get("status", JOB_STATUS_PENDING),
        }
    )


@router.get("/branding/jobs", response_model=BrandJobListResponse)
def list_branding_jobs(running_only: bool = False) -> BrandJobListResponse:
    """List branding run jobs, optionally restricted to in-flight ones.

    Preconditions:
        ``running_only`` is a boolean.
    Postconditions:
        Returns a ``BrandJobListResponse`` over all jobs, or only those in
        ``PENDING``/``RUNNING`` status when ``running_only`` is true. Each item is
        validated with the same explicit-default pattern as
        ``get_branding_job_status`` (missing ``job_id``/``status`` defaulted rather
        than raising).
    """
    statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    # Same model_validate + explicit-default pattern as get_branding_job_status.
    items = [
        BrandJobListItem.model_validate(
            {**j, "job_id": j.get("job_id", ""), "status": j.get("status", JOB_STATUS_PENDING)}
        )
        for j in list_jobs(statuses=statuses)
    ]
    return BrandJobListResponse(jobs=items)


@router.post("/branding/jobs/{job_id}/cancel")
def cancel_branding_job(job_id: str) -> Dict[str, Any]:
    """Request cancellation of a branding run job.

    Preconditions:
        ``job_id`` is a non-empty path string.
    Postconditions:
        When the job is cancellable, marks it cancelled, delivers the cancel
        signal through ``main._signal_branding_cancel`` (the hub's re-exported,
        interceptable binding), and returns ``{"job_id": job_id, "status":
        JOB_STATUS_CANCELLED, "success": True}``. When the job cannot be cancelled
        in its current status, returns a dict with ``"success": False`` and an
        explanatory ``"message"`` (and the job's current ``"status"``) and no
        state change. Raises 404 "Job not found" when the job is unknown.
    """
    from branding_team.api import main as _main

    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if cancel_job(job_id):
        # Call the hub's re-exported binding (main._signal_branding_cancel),
        # not _bg's module-local one — main re-exports this function
        # specifically so it can be intercepted/patched at the hub; calling
        # _bg directly would bypass that and always deliver a real signal.
        _main._signal_branding_cancel(job_id)
        return {"job_id": job_id, "status": JOB_STATUS_CANCELLED, "success": True}
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "success": False,
        "message": f"Cannot cancel job in status {data.get('status')}",
    }


@router.delete("/branding/jobs/{job_id}")
def delete_branding_job(job_id: str) -> Dict[str, Any]:
    """Delete a branding run job's record.

    Preconditions:
        ``job_id`` is a non-empty path string.
    Postconditions:
        Returns ``{"job_id": job_id, "deleted": True}`` once the job record is
        removed.
        Raises 404 "Job not found" when the job does not exist or the store
        reports no row deleted.
    """
    if get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "deleted": True}
