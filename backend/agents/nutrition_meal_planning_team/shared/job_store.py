"""
Job store for Nutrition & Meal Planning team: persists async job status via CentralJobManager.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared_job_management import CentralJobManager

logger = logging.getLogger(__name__)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache")).resolve()

_manager_instance: Optional[CentralJobManager] = None


def _manager(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> CentralJobManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = CentralJobManager(
            team="nutrition_meal_planning_team",
            cache_dir=cache_dir,
        )
    return _manager_instance


def create_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **fields: Any,
) -> None:
    """Create a new job with pending status."""
    _manager(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **fields)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data, or None if not found."""
    data = _manager(cache_dir).get_job(job_id)
    return copy.deepcopy(data) if data else None


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **fields: Any,
) -> None:
    """Update job fields."""
    _manager(cache_dir).update_job(job_id, **fields)


def list_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """List jobs, optionally filtered by status."""
    return _manager(cache_dir).list_jobs(statuses=statuses)


def cancel_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Request cancellation: set status to cancelled. Returns True if job existed and was updated."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return False
    _manager(cache_dir).update_job(job_id, status=JOB_STATUS_CANCELLED, heartbeat=False)
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
        jobs = list_jobs(cache_dir=cache_dir, statuses=[JOB_STATUS_PENDING, JOB_STATUS_RUNNING])
        for job in jobs:
            job_id = job.get("job_id")
            if job_id:
                update_job(job_id, status=JOB_STATUS_FAILED, error=reason, cache_dir=cache_dir)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)
