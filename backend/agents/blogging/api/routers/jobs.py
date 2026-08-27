"""Blogging API — job lifecycle: status, SSE stream, cancel, delete, resume, restart,
approve/unapprove, and listing."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.blogging.api.dependencies import get_job
from agents.blogging.api.models import (
    BlogJobListItem,
    BlogJobStatusResponse,
    CancelJobResponse,
    DeleteJobResponse,
    FullPipelineRequest,
    StartPipelineResponse,
    _blog_job_dict_to_status_response,
    _format_audience,
)
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from job_service_client import RESTARTABLE_STATUSES, RESUMABLE_STATUSES, validate_job_for_action

router = APIRouter()

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "needs_human_review"})
_BLOG_RESTARTABLE = RESTARTABLE_STATUSES | {"needs_human_review"}


def _reset_research_state(work_dir: Optional[str]) -> None:
    """Delete a job's research checkpoint cache and packet so a restart is genuinely
    from scratch.

    A restart reuses the job's ``work_dir`` and (for an unchanged brief) the same
    ``ResearchAgent`` cache key, so without clearing the cache the "from scratch"
    restart would silently resume the previous run's research checkpoints instead
    of re-running research. Separately, the artifact list/read endpoints expose
    ``research_packet.md`` purely based on its existence in ``work_dir``, so leaving
    the old one in place would serve stale research while the new run is pending —
    or permanently, if the new attempt fails before it gets a chance to overwrite it.

    A missing cache directory or artifact file (nothing to clear) is a no-op for
    that item; any other deletion failure propagates — reporting the restart as
    successful while stale research state actually survives would be worse than
    failing the restart outright.
    """
    if not work_dir:
        return
    work_path = Path(work_dir)
    try:
        shutil.rmtree(work_path / ".research_cache")
    except FileNotFoundError:
        pass
    try:
        (work_path / "research_packet.md").unlink()
    except FileNotFoundError:
        pass


@router.get(
    "/job/{job_id}",
    response_model=BlogJobStatusResponse,
    summary="Get job status",
    description="Poll the status of a running or completed pipeline job.",
)
def get_job_status(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> BlogJobStatusResponse:
    """Get the current status of a pipeline job."""
    return _blog_job_dict_to_status_response(job, job_id)


@router.get(
    "/job/{job_id}/stream",
    summary="Stream job status via SSE",
    description=(
        "Server-Sent Events stream for real-time job updates. "
        "Emits an initial 'snapshot' event with full status, then incremental 'update' events, "
        "and a terminal event ('complete', 'error', or 'cancelled') before closing."
    ),
)
def stream_job_status(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> StreamingResponse:
    """SSE stream for a pipeline job. Falls back gracefully if job is already terminal."""
    from agents.blogging.api import main as _main
    from agents.blogging.shared.job_event_bus import subscribe, unsubscribe

    from shared.sse import sse_job_stream_sync, sse_line

    def _snapshot_event() -> dict:
        current = _main.get_blog_job(job_id) or {}
        resp = _blog_job_dict_to_status_response(current, job_id)
        return {"type": "snapshot", **resp.model_dump(mode="json")}

    # If the job is already terminal, send a snapshot + done and close immediately.
    if job.get("status") in _TERMINAL_STATUSES:

        def _terminal_gen():
            yield sse_line(_snapshot_event())
            yield sse_line({"type": "done"})

        return StreamingResponse(_terminal_gen(), media_type="text/event-stream")

    return StreamingResponse(
        sse_job_stream_sync(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id=job_id,
            snapshot=_snapshot_event,
            terminal_types=("complete", "error", "cancelled"),
        ),
        media_type="text/event-stream",
    )


@router.post(
    "/job/{job_id}/cancel",
    response_model=CancelJobResponse,
    summary="Cancel a running or pending job",
    description="Sets job status to cancelled. Only allowed for pending or running jobs. Returns 400 for terminal states, 404 if job not found.",
)
def cancel_job(job_id: str) -> CancelJobResponse:
    """Request cancellation for a pending or running pipeline job."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None or _main.update_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = _main.get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current = job.get("status", "pending")
    if current not in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {current}. Cannot cancel.",
        )
    _main.update_blog_job(job_id, status="cancelled")
    return CancelJobResponse(job_id=job_id, message="Job cancellation requested.")


@router.delete(
    "/job/{job_id}",
    response_model=DeleteJobResponse,
    summary="Delete a job",
    description="Remove the job from the store. Returns 404 if job not found.",
)
def delete_job(job_id: str) -> DeleteJobResponse:
    """Delete a pipeline job by id."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None or _main.delete_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = _main.get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not _main.delete_blog_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteJobResponse(job_id=job_id, message="Job deleted.")


@router.post(
    "/job/{job_id}/resume",
    response_model=StartPipelineResponse,
    summary="Resume an interrupted blog pipeline job",
    description=(
        "Re-dispatch the pipeline for an interrupted job. The pipeline re-runs with "
        "the same inputs and work_dir, leveraging existing artifacts (planning cache, "
        "draft files) to skip completed work where possible."
    ),
)
def resume_blog_job(job_id: str) -> StartPipelineResponse:
    """Resume a blog job from its last checkpoint."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None or _main.update_blog_job is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    try:
        job = validate_job_for_action(
            _main.get_blog_job(job_id), job_id, RESUMABLE_STATUSES, "resumed"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    payload = job.get("request_payload")
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="Original request payload not available for resume."
        )

    _main.update_blog_job(
        job_id, status="running", error=None, failed_phase=None, status_text="Resuming..."
    )

    request = FullPipelineRequest(**payload)

    try:
        from agents.blogging.temporal.start_workflow import start_full_pipeline_workflow

        from shared.temporal.client import is_temporal_enabled

        if is_temporal_enabled():
            request_dict = request.model_dump(mode="json")
            audience_str = _format_audience(request.audience)
            request_dict["audience"] = audience_str or request_dict.get("audience")
            start_full_pipeline_workflow(job_id, request_dict)
            return StartPipelineResponse(job_id=job_id, message="Job resumed (Temporal)")
    except ImportError:
        pass

    _main._submit_async_job(_main._run_pipeline_with_tracking, job_id, request)
    return StartPipelineResponse(job_id=job_id, message="Job resumed")


@router.post(
    "/job/{job_id}/restart",
    response_model=StartPipelineResponse,
    summary="Restart a blog pipeline job from scratch",
    description="Reset the job and re-run the full pipeline with the same inputs.",
)
def restart_blog_job(job_id: str) -> StartPipelineResponse:
    """Restart a blog job from the beginning."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None or _main.update_blog_job is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    try:
        job = validate_job_for_action(
            _main.get_blog_job(job_id), job_id, _BLOG_RESTARTABLE, "restarted"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    payload = job.get("request_payload")
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="Original request payload not available for restart."
        )

    # Reset research state before resetting the job record: if this fails, the job
    # is left untouched (still in its prior terminal state) rather than reset to
    # "pending" with a restart that never actually happened.
    try:
        _reset_research_state(job.get("work_dir"))
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to reset research state for restart: {exc}"
        ) from exc

    from agents.blogging.shared.blog_job_store import reset_blog_job

    reset_blog_job(job_id)

    request = FullPipelineRequest(**payload)

    try:
        from agents.blogging.temporal.start_workflow import start_full_pipeline_workflow

        from shared.temporal.client import is_temporal_enabled

        if is_temporal_enabled():
            request_dict = request.model_dump(mode="json")
            audience_str = _format_audience(request.audience)
            request_dict["audience"] = audience_str or request_dict.get("audience")
            start_full_pipeline_workflow(job_id, request_dict)
            return StartPipelineResponse(job_id=job_id, message="Job restarted (Temporal)")
    except ImportError:
        pass

    _main._submit_async_job(_main._run_pipeline_with_tracking, job_id, request)
    return StartPipelineResponse(job_id=job_id, message="Job restarted from scratch")


@router.post(
    "/job/{job_id}/approve",
    response_model=BlogJobStatusResponse,
    summary="Approve a completed job",
    description="Mark the job as approved. Only allowed when status is completed or needs_human_review. Returns 400 for other statuses.",
)
def approve_job(job_id: str) -> BlogJobStatusResponse:
    """Approve a pipeline job (only for completed or needs_human_review)."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None or _main.approve_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = _main.get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    status = job.get("status", "")
    if status not in (_main.JOB_STATUS_COMPLETED, _main.JOB_STATUS_NEEDS_REVIEW):
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be approved: status is {status!r}. Only completed or needs_human_review jobs can be approved.",
        )
    _main.approve_blog_job(job_id)
    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between approve and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after approve")
    return _blog_job_dict_to_status_response(updated, job_id)


@router.post(
    "/job/{job_id}/unapprove",
    response_model=BlogJobStatusResponse,
    summary="Unapprove a job",
    description="Clear the approval for a job. Returns updated job status.",
)
def unapprove_job(job_id: str) -> BlogJobStatusResponse:
    """Clear approval for a pipeline job."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None or _main.unapprove_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = _main.get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    _main.unapprove_blog_job(job_id)
    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between unapprove and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after unapprove")
    return _blog_job_dict_to_status_response(updated, job_id)


@router.get(
    "/jobs",
    response_model=List[BlogJobListItem],
    summary="List jobs",
    description="List all pipeline jobs, optionally filtering to running jobs only.",
)
def list_jobs(running_only: bool = False) -> List[BlogJobListItem]:
    """List pipeline jobs."""
    from agents.blogging.api import main as _main

    if _main.list_blog_jobs is None:
        raise HTTPException(
            status_code=501,
            detail="Job listing not available - job store module not found",
        )

    jobs = _main.list_blog_jobs(running_only=running_only)
    return [
        BlogJobListItem(
            job_id=job.get("job_id", ""),
            status=job.get("status", "pending"),
            brief=job.get("brief", ""),
            phase=job.get("phase"),
            progress=job.get("progress", 0),
            created_at=job.get("created_at"),
            job_type=job.get("job_type"),
        )
        for job in jobs
    ]
