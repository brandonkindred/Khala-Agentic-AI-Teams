"""
File-based job store for async provisioning execution with progress tracking.

Thread-safe persistence of job state to .agent_cache/provisioning_jobs/{job_id}.json
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

DEFAULT_CACHE_DIR = Path(".agent_cache/provisioning_jobs")

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _job_file(job_id: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Get the path to a job's JSON file."""
    return cache_dir / f"{job_id}.json"


def _ensure_cache_dir(cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
    """Ensure the cache directory exists."""
    cache_dir.mkdir(parents=True, exist_ok=True)


def _read_job_file(path: Path) -> Optional[Dict[str, Any]]:
    """Read and parse a job file, returning None if it doesn't exist."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return None


def create_job(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier: str = "standard",
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Create a new provisioning job with initial state."""
    _ensure_cache_dir(cache_dir)
    
    data: Dict[str, Any] = {
        "job_id": job_id,
        "agent_id": agent_id,
        "manifest_path": manifest_path,
        "access_tier": access_tier,
        "status": JOB_STATUS_PENDING,
        "progress": 0,
        "current_phase": None,
        "current_tool": None,
        "tools_completed": 0,
        "tools_total": 0,
        "completed_phases": [],
        "phase_results": {},
        "error": None,
        "result": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    
    with _lock:
        _job_file(job_id, cache_dir).write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )


def update_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges kwargs into existing job data."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path)
        if data is None:
            return
        
        for key, value in kwargs.items():
            if value is not None:
                data[key] = value
        
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        path.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )


def get_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Dict[str, Any]:
    """Get job data by ID. Returns empty dict if not found."""
    with _lock:
        data = _read_job_file(_job_file(job_id, cache_dir))
        return data if data is not None else {}


def list_jobs(
    running_only: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    """List all jobs, optionally filtered to running/pending only."""
    _ensure_cache_dir(cache_dir)
    jobs: List[Dict[str, Any]] = []
    
    with _lock:
        for job_file in cache_dir.glob("*.json"):
            data = _read_job_file(job_file)
            if data is None:
                continue
            
            if running_only:
                if data.get("status") in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING):
                    jobs.append(data)
            else:
                jobs.append(data)
    
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


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
    update_job(
        job_id,
        cache_dir,
        status=JOB_STATUS_COMPLETED,
        progress=100,
        result=result,
    )


def mark_job_failed(
    job_id: str,
    error: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as failed with error message."""
    update_job(
        job_id,
        cache_dir,
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
    
    update_job(job_id, cache_dir, **updates)


def add_completed_phase(
    job_id: str,
    phase: str,
    phase_result: Optional[Dict[str, Any]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Add a phase to the completed phases list."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path)
        if data is None:
            return
        
        completed = data.get("completed_phases", [])
        if phase not in completed:
            completed.append(phase)
        data["completed_phases"] = completed
        
        if phase_result is not None:
            phase_results = data.get("phase_results", {})
            phase_results[phase] = phase_result
            data["phase_results"] = phase_results
        
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        path.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )


def delete_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Delete a job file. Returns True if deleted, False if not found."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        if path.exists():
            path.unlink()
            return True
        return False
