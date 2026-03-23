"""
Job store for AI Systems team: persists async job status via the job service.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
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


def _client(cache_dir: Path | str = DEFAULT_CACHE_DIR) -> JobServiceClient:
    return JobServiceClient(team="ai_systems_team", cache_dir=str(cache_dir))


def create_job(
    job_id: str,
    project_name: str,
    spec_path: str,
    constraints: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Create a new AI system build job with initial state."""
    now = datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = {
        "project_name": project_name,
        "spec_path": spec_path,
        "constraints": constraints or {},
        "output_dir": output_dir,
        "progress": 0,
        "current_phase": None,
        "completed_phases": [],
        "phase_results": {},
        "blueprint": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "events": [],
    }
    _client(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **data)


def get_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Dict[str, Any]:
    """Get job data by ID. Returns empty dict if not found."""
    return _client(cache_dir).get_job(job_id) or {}


def update_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges kwargs into existing job data."""
    _client(cache_dir).update_job(job_id, **kwargs)


def list_jobs(
    running_only: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    """List all jobs, optionally filtered to running/pending only."""
    statuses: Optional[List[str]] = (
        [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    )
    return _client(cache_dir).list_jobs(statuses=statuses)


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running AI systems jobs as failed (e.g. on server shutdown)."""
    try:
        _client(cache_dir).mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def cancel_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Set job status to cancelled. Returns True if job existed and was updated."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return False
    _client(cache_dir).update_job(job_id, status=JOB_STATUS_CANCELLED, heartbeat=False)
    return True


def mark_job_running(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as running."""
    update_job(job_id, cache_dir, status=JOB_STATUS_RUNNING)


def mark_job_completed(
    job_id: str,
    blueprint: Optional[Dict[str, Any]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as completed with optional blueprint."""
    updates: Dict[str, Any] = {"status": JOB_STATUS_COMPLETED, "progress": 100}
    if blueprint is not None:
        updates["blueprint"] = blueprint
    update_job(job_id, cache_dir=cache_dir, **updates)


def mark_job_failed(
    job_id: str,
    error: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as failed with error message."""
    update_job(job_id, cache_dir=cache_dir, status=JOB_STATUS_FAILED, error=error)


def update_phase_progress(
    job_id: str,
    current_phase: str,
    progress: int,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Update job with current phase progress."""
    update_job(
        job_id,
        cache_dir=cache_dir,
        current_phase=current_phase,
        progress=progress,
    )


def add_completed_phase(
    job_id: str,
    phase: str,
    phase_result: Optional[Dict[str, Any]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Add a phase to the completed phases list."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return
    completed = list(data.get("completed_phases", []))
    if phase not in completed:
        completed.append(phase)
    updates: Dict[str, Any] = {"completed_phases": completed}
    if phase_result is not None:
        phase_results = dict(data.get("phase_results", {}))
        phase_results[phase] = phase_result
        updates["phase_results"] = phase_results
    update_job(job_id, cache_dir=cache_dir, **updates)


def delete_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Delete a job. Returns True if deleted, False if not found."""
    return _client(cache_dir).delete_job(job_id)
