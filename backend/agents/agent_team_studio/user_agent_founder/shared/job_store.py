"""Job store for the user_agent_founder team — backed by JobServiceClient.

Centralises access to the shared job service so the API layer, the
Temporal activity, and the orchestrator all speak to the same singleton.
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
    RESTARTABLE_STATUSES,
    RESUMABLE_STATUSES,
    JobServiceClient,
    validate_job_for_action,
)

__all__ = [
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "RESTARTABLE_STATUSES",
    "RESUMABLE_STATUSES",
    "create_job",
    "delete_job",
    "get_job",
    "list_jobs",
    "reset_job",
    "update_job",
    "validate_job_for_action",
]

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))

_client_instance: Optional[JobServiceClient] = None


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(team="user_agent_founder")
    return _client_instance


def create_job(job_id: str, *, status: str = JOB_STATUS_PENDING, **fields: Any) -> None:
    _client().create_job(job_id, status=status, **fields)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _client().get_job(job_id)


def update_job(job_id: str, **fields: Any) -> None:
    _client().update_job(job_id, **fields)


def list_jobs(statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    return _client().list_jobs(statuses=statuses)


def delete_job(job_id: str) -> bool:
    return bool(_client().delete_job(job_id))


def reset_job(job_id: str) -> None:
    """Clear terminal-state fields so a job can be restarted from scratch."""
    _client().update_job(
        job_id,
        status=JOB_STATUS_PENDING,
        error=None,
        current_phase="starting",
    )
