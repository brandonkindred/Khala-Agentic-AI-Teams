"""Job store for Road Trip Planning team: persists async job status via the job service."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_service_client import JobServiceClient

logger = logging.getLogger(__name__)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))

_client_instance: Optional[JobServiceClient] = None


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(
            team="road_trip_planning_team",
            cache_dir=str(cache_dir),
        )
    return _client_instance


def create_job(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR, **fields: Any) -> None:
    _client(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **fields)


def get_job(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Optional[Dict[str, Any]]:
    return _client(cache_dir).get_job(job_id)


def update_job(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR, **fields: Any) -> None:
    _client(cache_dir).update_job(job_id, **fields)


def list_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR, statuses: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    return _client(cache_dir).list_jobs(statuses=statuses)


def mark_all_running_jobs_failed(reason: str, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> None:
    """Mark all pending or running jobs as failed (e.g. on server shutdown)."""
    try:
        _client(cache_dir).mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)
