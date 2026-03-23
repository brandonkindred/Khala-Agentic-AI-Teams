"""
Job store for Planning V3 API: persists job status via the job service.
"""

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

DEFAULT_CACHE_DIR: Path = Path(os.getenv("AGENT_CACHE", ".agent_cache"))
_client_instance: Optional[JobServiceClient] = None


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(
            team="planning_v3_team",
            cache_dir=str(cache_dir),
        )
    return _client_instance


def create_job(
    job_id: str,
    repo_path: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **fields: Any,
) -> None:
    data: Dict[str, Any] = {
        "repo_path": repo_path,
        "progress": 0,
        "current_phase": None,
        "status_text": None,
        "error": None,
        "handoff_package": None,
        "pending_questions": [],
        "waiting_for_answers": False,
        "job_type": "planning_v3",
        "events": [],
    }
    data.update(fields)
    _client(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **data)


def get_job(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Optional[Dict[str, Any]]:
    return _client(cache_dir).get_job(job_id)


def update_job(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR, **fields: Any) -> None:
    _client(cache_dir).update_job(job_id, **fields)


def list_jobs(
    running_only: bool = False,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    statuses: Optional[List[str]] = (
        [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    )
    return _client(cache_dir).list_jobs(statuses=statuses) or []


def mark_job_completed(
    job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR, **fields: Any
) -> None:
    _client(cache_dir).update_job(
        job_id, status=JOB_STATUS_COMPLETED, progress=100, heartbeat=False, **fields
    )


def mark_job_failed(job_id: str, error: str, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> None:
    _client(cache_dir).update_job(job_id, status=JOB_STATUS_FAILED, error=error, heartbeat=False)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Called on shutdown to mark running jobs as failed."""
    try:
        _client().mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)
