"""
Job store for Nutrition & Meal Planning team: persists async job status via the job service.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_service_client import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)

# Re-export so other modules (api/main.py, temporal/activities.py) can import from here.
__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
]

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))

_client_instance: Optional[JobServiceClient] = None


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(
            team="nutrition_meal_planning_team",
            cache_dir=str(cache_dir),
        )
    return _client_instance


def create_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **fields: Any,
) -> None:
    """Create a new job with pending status."""
    _client(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **fields)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data, or None if not found."""
    return _client(cache_dir).get_job(job_id)


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **fields: Any,
) -> None:
    """Update job fields."""
    _client(cache_dir).update_job(job_id, **fields)


def list_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """List jobs, optionally filtered by status."""
    return _client(cache_dir).list_jobs(statuses=statuses)


def cancel_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Request cancellation: set status to cancelled. Returns True if job existed and was updated."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return False
    _client(cache_dir).update_job(job_id, status=JOB_STATUS_CANCELLED, heartbeat=False)
    return True


def is_job_cancelled(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the job exists and has status cancelled."""
    data = get_job(job_id, cache_dir=cache_dir)
    return data is not None and data.get("status") == JOB_STATUS_CANCELLED


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running jobs as failed (e.g. on server shutdown)."""
    try:
        _client(cache_dir).mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)
