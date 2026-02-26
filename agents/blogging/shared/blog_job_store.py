"""
Job store for blogging pipeline: persists job status and progress per job in a cache directory.

Each job is stored as {cache_dir}/blog_jobs/{job_id}.json so state survives process restarts.
This enables async API endpoints with polling for UI progress tracking.
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

# Job status constants
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_NEEDS_REVIEW = "needs_human_review"

DEFAULT_CACHE_DIR: str | Path = ".agent_cache"
_lock = threading.Lock()


def _jobs_dir(cache_dir: str | Path) -> Path:
    path = Path(cache_dir) / "blog_jobs"
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


def create_blog_job(
    job_id: str,
    brief: str,
    *,
    audience: Optional[str] = None,
    tone_or_purpose: Optional[str] = None,
    work_dir: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Create a new blog job with pending status and persist to cache."""
    data: Dict[str, Any] = {
        "job_id": job_id,
        "brief": brief,
        "audience": audience,
        "tone_or_purpose": tone_or_purpose,
        "work_dir": work_dir,
        "status": JOB_STATUS_PENDING,
        "phase": None,
        "progress": 0,
        "status_text": "Initializing...",
        "error": None,
        "failed_phase": None,
        "title_choices": [],
        "outline": None,
        "draft_preview": None,
        "research_sources_count": 0,
        "draft_iterations": 0,
        "rewrite_iterations": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
    }
    with _lock:
        _job_file(job_id, cache_dir).write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )


def get_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data from cache, or None if not found."""
    with _lock:
        data = _read_job_file(_job_file(job_id, cache_dir))
        return copy.deepcopy(data) if data else None


def list_blog_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
) -> List[Dict[str, Any]]:
    """List blog jobs from cache. If running_only is True, only include pending or running."""
    running_statuses = (JOB_STATUS_PENDING, JOB_STATUS_RUNNING)
    result: List[Dict[str, Any]] = []
    jobs_path = _jobs_dir(cache_dir)
    if not jobs_path.exists():
        return result
    with _lock:
        for path in jobs_path.glob("*.json"):
            job_id = path.stem
            data = _read_job_file(path)
            if not data:
                continue
            status = data.get("status", JOB_STATUS_PENDING)
            if running_only and status not in running_statuses:
                continue
            result.append({
                "job_id": job_id,
                "status": status,
                "brief": data.get("brief", "")[:100],
                "phase": data.get("phase"),
                "progress": data.get("progress", 0),
                "created_at": data.get("created_at"),
            })
    return result


def update_blog_job(
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


def start_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as running with start timestamp."""
    update_blog_job(
        job_id,
        cache_dir=cache_dir,
        status=JOB_STATUS_RUNNING,
        started_at=datetime.now(timezone.utc).isoformat(),
    )


def complete_blog_job(
    job_id: str,
    *,
    status: str = JOB_STATUS_COMPLETED,
    title_choices: Optional[List[Dict[str, Any]]] = None,
    outline: Optional[str] = None,
    draft_preview: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as completed with final results."""
    kwargs: Dict[str, Any] = {
        "status": status,
        "phase": "finalize",
        "progress": 100,
        "status_text": "Pipeline complete" if status == JOB_STATUS_COMPLETED else "Needs human review",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if title_choices is not None:
        kwargs["title_choices"] = title_choices
    if outline is not None:
        kwargs["outline"] = outline
    if draft_preview is not None:
        kwargs["draft_preview"] = draft_preview
    update_blog_job(job_id, cache_dir=cache_dir, **kwargs)


def fail_blog_job(
    job_id: str,
    error: str,
    *,
    failed_phase: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as failed with error details."""
    update_blog_job(
        job_id,
        cache_dir=cache_dir,
        status=JOB_STATUS_FAILED,
        error=error,
        failed_phase=failed_phase,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


def delete_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Delete a job from the cache. Returns True if deleted, False if not found."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        if path.exists():
            path.unlink()
            return True
        return False
