"""
Job store for async API: persists job status and progress per job in a cache directory.
Each job is stored as {cache_dir}/jobs/{job_id}.json so state survives process restarts.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
# Agent process crashed (NameError, ImportError, etc.) - distinct from build/LLM failure
JOB_STATUS_AGENT_CRASH = "agent_crash"

DEFAULT_CACHE_DIR: str | Path = ".agent_cache"
_lock = threading.Lock()


def _jobs_dir(cache_dir: str | Path) -> Path:
    path = Path(cache_dir) / "jobs"
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
    repo_path: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Create a new job with pending status and persist to cache."""
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
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_task_result(
    job_id: str,
    result: Dict[str, Any],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Append a task result to the job and persist to cache."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        results = data.get("task_results", [])
        results.append(result)
        data["task_results"] = results
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
