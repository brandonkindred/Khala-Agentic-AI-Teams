"""
Job store for Agent Provisioning team: persists async job status via CentralJobManager.

Jobs are stored under {cache_dir}/agent_provisioning_team/jobs/{job_id}.json
"""

from __future__ import annotations

import copy
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared_job_management import CentralJobManager

logger = logging.getLogger(__name__)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))


def _manager(cache_dir: Path | str = DEFAULT_CACHE_DIR) -> CentralJobManager:
    return CentralJobManager(team="agent_provisioning_team", cache_dir=cache_dir)


def create_job(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier: str = "standard",
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Create a new provisioning job with initial state."""
    now = datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = {
        "agent_id": agent_id,
        "manifest_path": manifest_path,
        "access_tier": access_tier,
        "progress": 0,
        "current_phase": None,
        "current_tool": None,
        "tools_completed": 0,
        "tools_total": 0,
        "completed_phases": [],
        "phase_results": {},
        "error": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    _manager(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **data)


def get_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Dict[str, Any]:
    """Get job data by ID. Returns empty dict if not found."""
    data = _manager(cache_dir).get_job(job_id)
    return copy.deepcopy(data) if data else {}


def update_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges kwargs into existing job data."""
    _manager(cache_dir).update_job(job_id, **kwargs)


def list_jobs(
    running_only: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    """List all jobs, optionally filtered to running/pending only."""
    statuses: Optional[List[str]] = (
        [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    )
    return _manager(cache_dir).list_jobs(statuses=statuses)


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running provisioning jobs as failed (e.g. on server shutdown)."""
    try:
        jobs = list_jobs(running_only=True, cache_dir=cache_dir)
        for job in jobs:
            job_id = job.get("job_id")
            if job_id:
                update_job(job_id, cache_dir=cache_dir, status=JOB_STATUS_FAILED, error=reason)
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
    _manager(cache_dir).update_job(job_id, status=JOB_STATUS_CANCELLED, heartbeat=False)
    return True


def mark_job_running(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as running."""
    update_job(job_id, cache_dir, status=JOB_STATUS_RUNNING)


def mark_job_completed(
    job_id: str,
    result: Optional[Dict[str, Any]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as completed with optional result."""
    updates: Dict[str, Any] = {"status": JOB_STATUS_COMPLETED, "progress": 100}
    if result is not None:
        updates["result"] = result
    update_job(job_id, cache_dir=cache_dir, **updates)


def mark_job_failed(
    job_id: str,
    error: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as failed with error message."""
    update_job(
        job_id,
        cache_dir=cache_dir,
        status=JOB_STATUS_FAILED,
        error=error,
    )


def update_phase_progress(
    job_id: str,
    current_phase: str,
    progress: int,
    current_tool: Optional[str] = None,
    tools_completed: Optional[int] = None,
    tools_total: Optional[int] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Update job with current phase progress."""
    updates: Dict[str, Any] = {
        "current_phase": current_phase,
        "progress": progress,
    }
    if current_tool is not None:
        updates["current_tool"] = current_tool
    if tools_completed is not None:
        updates["tools_completed"] = tools_completed
    if tools_total is not None:
        updates["tools_total"] = tools_total
    update_job(job_id, cache_dir=cache_dir, **updates)


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
    """Delete a job file. Returns True if deleted, False if not found."""
    return _manager(cache_dir).delete_job(job_id)
