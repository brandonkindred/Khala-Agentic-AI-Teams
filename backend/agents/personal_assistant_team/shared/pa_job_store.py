"""
Job store for Personal Assistant API: persists job status and progress via CentralJobManager.

Jobs are stored under {cache_dir}/personal_assistant_team/jobs/{job_id}.json
"""

from __future__ import annotations

import copy
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared_job_management import job_manager_for_team, maybe_start_job_heartbeat

logger = logging.getLogger(__name__)

PA_JOB_STATUS_PENDING = "pending"
PA_JOB_STATUS_RUNNING = "running"
PA_JOB_STATUS_COMPLETED = "completed"
PA_JOB_STATUS_FAILED = "failed"
PA_JOB_STATUS_CANCELLED = "cancelled"

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache")).resolve()


def _manager(cache_dir: str | Path = DEFAULT_CACHE_DIR):
    return job_manager_for_team("personal_assistant_team", cache_dir=cache_dir)


def create_job(
    job_id: str,
    user_id: str,
    request_type: str,
    message: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Create a new job with pending status and persist to cache."""
    now = datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = {
        "job_id": job_id,
        "user_id": user_id,
        "status": PA_JOB_STATUS_PENDING,
        "request_type": request_type,
        "request_message": message,
        "context": context or {},
        "progress": 0,
        "status_text": None,
        "response": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    fields = {k: v for k, v in data.items() if k not in ("job_id", "status")}
    _manager(cache_dir).create_job(job_id, status=PA_JOB_STATUS_PENDING, **fields)
    maybe_start_job_heartbeat(job_id, team="personal_assistant_team", cache_dir=cache_dir)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data from cache, or None if not found."""
    data = _manager(cache_dir).get_job(job_id)
    return copy.deepcopy(data) if data else None


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges with existing data and persists to cache."""
    _manager(cache_dir).update_job(job_id, **kwargs)


def list_jobs(
    user_id: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List jobs from cache. Optionally filter by user_id and running status."""
    statuses: Optional[List[str]] = (
        [PA_JOB_STATUS_PENDING, PA_JOB_STATUS_RUNNING] if running_only else None
    )
    raw = _manager(cache_dir).list_jobs(statuses=statuses)
    result: List[Dict[str, Any]] = []
    for data in raw:
        if user_id is not None and data.get("user_id") != user_id:
            continue
        result.append({
            "job_id": data.get("job_id"),
            "user_id": data.get("user_id"),
            "status": data.get("status", PA_JOB_STATUS_PENDING),
            "request_type": data.get("request_type"),
            "progress": data.get("progress", 0),
            "status_text": data.get("status_text"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        })
    return result[:limit]


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running PA jobs as failed (e.g. on server shutdown)."""
    try:
        jobs = list_jobs(cache_dir=cache_dir, running_only=True, limit=10000)
        for job in jobs:
            job_id = job.get("job_id")
            if job_id:
                update_job(job_id, status=PA_JOB_STATUS_FAILED, error=reason, cache_dir=cache_dir)
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
    _manager(cache_dir).update_job(
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
    """Delete a job from the cache. Returns True if deleted, False if not found."""
    return _manager(cache_dir).delete_job(job_id)
