"""
Job store for async API: persists job status and progress in-memory.
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

# In-memory store: {job_id: {job data dict}}
_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def create_job(job_id: str, repo_path: str) -> None:
    """Create a new job with pending status."""
    data = {
        "job_id": job_id,
        "repo_path": repo_path,
        "status": JOB_STATUS_PENDING,
        "progress": 0,
        "current_task": None,
        "task_results": [],
        "execution_order": [],
        "error": None,
        "architecture_overview": None,
        "requirements_title": None,
    }
    with _lock:
        _store[job_id] = data


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Get job data or None if not found."""
    with _lock:
        data = _store.get(job_id)
        return copy.deepcopy(data) if data else None


def update_job(job_id: str, **kwargs: Any) -> None:
    """Update job fields. Merges with existing data."""
    with _lock:
        data = _store.get(job_id)
        if data is None:
            data = {}
            _store[job_id] = data
        data.update(kwargs)


def add_task_result(job_id: str, result: Dict[str, Any]) -> None:
    """Append a task result to the job."""
    with _lock:
        data = _store.get(job_id)
        if data is None:
            data = {}
            _store[job_id] = data
        results = data.get("task_results", [])
        results.append(result)
        data["task_results"] = results
