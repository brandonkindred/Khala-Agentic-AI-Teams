"""
Job store for Personal Assistant API: persists job status and progress.
Each job is stored as {cache_dir}/pa_jobs/{job_id}.json.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PA_JOB_STATUS_PENDING = "pending"
PA_JOB_STATUS_RUNNING = "running"
PA_JOB_STATUS_COMPLETED = "completed"
PA_JOB_STATUS_FAILED = "failed"
PA_JOB_STATUS_CANCELLED = "cancelled"

DEFAULT_CACHE_DIR: str | Path = ".agent_cache"
_lock = threading.Lock()


def _jobs_dir(cache_dir: str | Path) -> Path:
    path = Path(cache_dir) / "pa_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_file(job_id: str, cache_dir: str | Path) -> Path:
    return _jobs_dir(cache_dir) / f"{job_id}.json"


def _read_job_file(path: Path) -> Optional[Dict[str, Any]]:
    """Read job from path (caller must hold _lock if sharing with writers)."""
    if not path.exists():
        return None
    for attempt in range(2):
        try:
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                if attempt == 0:
                    time.sleep(0.1)
                    continue
                return None
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            if attempt == 0:
                time.sleep(0.1)
                continue
            logger.warning("Failed to read job file %s: %s", path, e)
            return None
        except Exception as e:
            logger.warning("Failed to read job file %s: %s", path, e)
            return None
    return None


def create_job(
    job_id: str,
    user_id: str,
    request_type: str,
    message: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Create a new job with pending status and persist to cache."""
    data: Dict[str, Any] = {
        "job_id": job_id,
        "user_id": user_id,
        "status": PA_JOB_STATUS_PENDING,
        "request_type": request_type,
        "request_message": message,
        "context": context or {},
        "progress": 0,
        "status_text": None,
        "response": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        _job_file(job_id, cache_dir).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data from cache, or None if not found."""
    with _lock:
        data = _read_job_file(_job_file(job_id, cache_dir))
        return copy.deepcopy(data) if data else None


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges with existing data and persists to cache."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        data.update(kwargs)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_jobs(
    user_id: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List jobs from cache. Optionally filter by user_id and running status."""
    running_statuses = (PA_JOB_STATUS_PENDING, PA_JOB_STATUS_RUNNING)
    result: List[Dict[str, Any]] = []
    jobs_path = _jobs_dir(cache_dir)
    if not jobs_path.exists():
        return result
    with _lock:
        for path in jobs_path.glob("*.json"):
            data = _read_job_file(path)
            if not data:
                continue
            if user_id is not None and data.get("user_id") != user_id:
                continue
            status = data.get("status", PA_JOB_STATUS_PENDING)
            if running_only and status not in running_statuses:
                continue
            result.append({
                "job_id": data.get("job_id"),
                "user_id": data.get("user_id"),
                "status": status,
                "request_type": data.get("request_type"),
                "progress": data.get("progress", 0),
                "status_text": data.get("status_text"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            })
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result[:limit]


def cancel_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Cancel a job if it's still pending or running. Returns True if cancelled."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path)
        if not data:
            return False
        status = data.get("status")
        if status not in (PA_JOB_STATUS_PENDING, PA_JOB_STATUS_RUNNING):
            return False
        data["status"] = PA_JOB_STATUS_CANCELLED
        data["status_text"] = "Job cancelled by user"
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True


def is_job_cancelled(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Check if a job has been cancelled."""
    data = get_job(job_id, cache_dir)
    return data is not None and data.get("status") == PA_JOB_STATUS_CANCELLED
