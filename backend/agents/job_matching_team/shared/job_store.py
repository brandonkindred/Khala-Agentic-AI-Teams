"""Async job tracking for the job matching team's scan jobs.

Thin wrapper over the central ``JobServiceClient`` (same pattern as
``startup_advisor/shared/job_store.py``). Used to submit a scan in the
background and poll its status from the HTTP API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from job_service_client import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)

__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "cancel_job",
    "create_job",
    "delete_job",
    "get_job",
    "is_job_cancelled",
    "list_jobs",
    "update_job",
]

logger = logging.getLogger(__name__)

_client_instance: Optional[JobServiceClient] = None


def _client() -> JobServiceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(team="job_matching")
    return _client_instance


def create_job(job_id: str, **fields: Any) -> None:
    _client().create_job(job_id, status=JOB_STATUS_PENDING, **fields)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _client().get_job(job_id)


def update_job(job_id: str, **fields: Any) -> None:
    _client().update_job(job_id, **fields)


def list_jobs(statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    return _client().list_jobs(statuses=statuses)


def cancel_job(job_id: str) -> bool:
    job = _client().get_job(job_id)
    if job is None or job.get("status") not in {JOB_STATUS_PENDING, JOB_STATUS_RUNNING}:
        return False
    _client().update_job(job_id, status=JOB_STATUS_CANCELLED)
    return True


def is_job_cancelled(job_id: str) -> bool:
    """Return True if the job exists and has been marked cancelled."""
    job = _client().get_job(job_id)
    return job is not None and job.get("status") == JOB_STATUS_CANCELLED


def delete_job(job_id: str) -> bool:
    return bool(_client().delete_job(job_id))
