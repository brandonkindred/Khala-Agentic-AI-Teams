"""
Job store for Personal Assistant API: persists job status and progress via the job service.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_service_client import JobServiceClient

logger = logging.getLogger(__name__)

PA_JOB_STATUS_PENDING = "pending"
PA_JOB_STATUS_RUNNING = "running"
PA_JOB_STATUS_COMPLETED = "completed"
PA_JOB_STATUS_FAILED = "failed"
PA_JOB_STATUS_CANCELLED = "cancelled"

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache")).resolve()


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    return JobServiceClient(team="personal_assistant_team", cache_dir=str(cache_dir))


def create_job(
    job_id: str,
    user_id: str,
    request_type: str,
    message: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Create a new job with pending status."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = {
        "user_id": user_id,
        "request_type": request_type,
        "request_message": message,
        "context": context or {},
        "progress": 0,
        "status_text": None,
        "response": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "events": [],
    }
    _client(cache_dir).create_job(job_id, status=PA_JOB_STATUS_PENDING, **data)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data, or None if not found."""
    return _client(cache_dir).get_job(job_id)


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges with existing data."""
    _client(cache_dir).update_job(job_id, **kwargs)


def list_jobs(
    user_id: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List jobs. Optionally filter by user_id and running status."""
    statuses: Optional[List[str]] = (
        [PA_JOB_STATUS_PENDING, PA_JOB_STATUS_RUNNING] if running_only else None
    )
    raw = _client(cache_dir).list_jobs(statuses=statuses)
    result: List[Dict[str, Any]] = []
    for data in raw:
        if user_id is not None and data.get("user_id") != user_id:
            continue
        result.append(
            {
                "job_id": data.get("job_id"),
                "user_id": data.get("user_id"),
                "status": data.get("status", PA_JOB_STATUS_PENDING),
                "request_type": data.get("request_type"),
                "progress": data.get("progress", 0),
                "status_text": data.get("status_text"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            }
        )
    return result[:limit]


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running PA jobs as failed (e.g. on server shutdown)."""
    try:
        _client(cache_dir).mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def cancel_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Cancel a job if it's still pending or running. Returns True if cancelled."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return False
    status = data.get("status")
    if status not in (PA_JOB_STATUS_PENDING, PA_JOB_STATUS_RUNNING):
        return False
    _client(cache_dir).update_job(
        job_id,
        status=PA_JOB_STATUS_CANCELLED,
        status_text="Job cancelled by user",
        heartbeat=False,
    )
    return True


def is_job_cancelled(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Check if a job has been cancelled."""
    data = get_job(job_id, cache_dir)
    return data is not None and data.get("status") == PA_JOB_STATUS_CANCELLED


def delete_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Delete a job. Returns True if deleted, False if not found."""
    return _client(cache_dir).delete_job(job_id)
