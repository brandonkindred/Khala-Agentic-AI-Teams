"""
Job store for Planning API: persists job status via the job service.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)
from user_profile import ArtifactType, record_association_safe

logger = logging.getLogger(__name__)

_client_instance: Optional[JobServiceClient] = None


def _client() -> JobServiceClient:
    """Return the process-wide `JobServiceClient` singleton for the Planning team."""
    global _client_instance
    if _client_instance is None:
        _client_instance = JobServiceClient(team="planning_team")
    return _client_instance


def create_job(job_id: str, repo_path: str, **fields: Any) -> None:
    """Create a new Planning job record.

    Preconditions: ``job_id`` is a non-empty, unique job id; ``repo_path`` is
    the resolved workspace path (may be blank for a label-only run).
    Postconditions: a job record exists with status ``pending`` and default
    Planning fields (progress, phase, handoff package, etc.), overridable via
    ``fields``. Best-effort links the job to the default user profile —
    a link failure never raises here.
    """
    data: Dict[str, Any] = {
        "repo_path": repo_path,
        "progress": 0,
        "current_phase": None,
        "status_text": None,
        "error": None,
        "handoff_package": None,
        "pending_questions": [],
        "waiting_for_answers": False,
        "job_type": "planning",
        "events": [],
    }
    data.update(fields)
    _client().create_job(job_id, status=JOB_STATUS_PENDING, **data)
    # Best-effort: link the project to the default profile. record_association_safe
    # never raises, so a link failure can't break job creation.
    record_association_safe(ArtifactType.PROJECT, "planning", job_id, label=repo_path or job_id)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the job record for ``job_id``, or None if it does not exist."""
    return _client().get_job(job_id)


def update_job(job_id: str, **fields: Any) -> None:
    """Merge ``fields`` into the job record for ``job_id``."""
    _client().update_job(job_id, **fields)


def list_jobs(running_only: bool = False) -> List[Dict[str, Any]]:
    """Return job records, optionally filtered to pending/running only."""
    statuses: Optional[List[str]] = (
        [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    )
    return _client().list_jobs(statuses=statuses) or []


def mark_job_completed(job_id: str, **fields: Any) -> None:
    """Mark the job completed at 100% progress, merging any extra ``fields``."""
    _client().update_job(
        job_id, status=JOB_STATUS_COMPLETED, progress=100, heartbeat=False, **fields
    )


def mark_job_failed(job_id: str, error: str) -> None:
    """Mark the job failed with the given ``error`` message."""
    _client().update_job(job_id, status=JOB_STATUS_FAILED, error=error, heartbeat=False)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Called on shutdown to mark running jobs as failed."""
    try:
        _client().mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)
