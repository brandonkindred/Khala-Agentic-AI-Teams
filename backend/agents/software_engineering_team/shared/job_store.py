"""
Job store for async API: persists job status and progress per job in a cache directory.

Uses AGENT_CACHE environment variable when set (otherwise .agent_cache), resolved to an
absolute path so list/create use the same directory regardless of process CWD. Each job
is stored under {cache_dir}/software_engineering_team/jobs/{job_id}.json via
CentralJobManager so state survives process restarts.
"""

from __future__ import annotations

import copy
import os
import json
import logging
import threading
import time
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
# Agent process crashed (NameError, ImportError, etc.) - distinct from build/LLM failure
JOB_STATUS_AGENT_CRASH = "agent_crash"
# Frontend (or other agent) could not reach LLM after retries; user must confirm connectivity and resume
JOB_STATUS_PAUSED_LLM_CONNECTIVITY = "paused_llm_connectivity"

# Sentinel failure reason when LLM is unreachable after 3 attempts (frontend team retry + circuit breaker)
LLM_UNREACHABLE_AFTER_RETRIES = (
    "LLM unreachable after 3 attempts with exponential backoff. Check connectivity and resume when ready."
)

DEFAULT_CACHE_DIR: Path = Path(os.getenv("AGENT_CACHE", ".agent_cache")).resolve()
_lock = threading.Lock()
_jobs_path_logged = False


def _manager(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> CentralJobManager:
    global _jobs_path_logged
    if not _jobs_path_logged:
        jobs_dir = Path(cache_dir) / "software_engineering_team" / "jobs"
        logger.info("Software engineering job store path: %s", jobs_dir)
        _jobs_path_logged = True
    return CentralJobManager(team="software_engineering_team", cache_dir=cache_dir)


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
    job_type: Optional[str] = None,
) -> None:
    """Create a new job with pending status and persist to cache."""
    data: Dict[str, Any] = {
        "job_id": job_id,
        "repo_path": repo_path,
        "status": JOB_STATUS_PENDING,
        "progress": 0,
        "current_task": None,
        "status_text": None,
        "task_results": [],
        "execution_order": [],
        "error": None,
        "architecture_overview": None,
        "requirements_title": None,
        "pending_questions": [],
        "waiting_for_answers": False,
        "submitted_answers": [],
        "cancel_requested": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if job_type is not None:
        data["job_type"] = job_type
    _manager(cache_dir).create_job(**data)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data from cache, or None if not found."""
    data = _manager(cache_dir).get_job(job_id)
    return copy.deepcopy(data) if data else None


def list_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
    job_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List jobs from cache. If running_only is True, only include pending or running.
    If job_type is set, only include jobs with that job_type."""
    running_statuses = (JOB_STATUS_PENDING, JOB_STATUS_RUNNING)
    result: List[Dict[str, Any]] = []
    statuses = list(running_statuses) if running_only else None
    jobs = _manager(cache_dir).list_jobs(statuses=statuses)
    for data in jobs:
        if job_type is not None and data.get("job_type") != job_type:
            continue
        result.append({
            "job_id": data.get("job_id", ""),
            "status": data.get("status", JOB_STATUS_PENDING),
            "repo_path": data.get("repo_path"),
            "job_type": data.get("job_type"),
            "created_at": data.get("created_at"),
        })
    return result


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running jobs as failed (e.g. on server shutdown)."""
    try:
        jobs = list_jobs(cache_dir=cache_dir, running_only=True)
        for job in jobs:
            job_id = job.get("job_id")
            if job_id:
                update_job(job_id, status=JOB_STATUS_FAILED, error=reason, cache_dir=cache_dir)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def mark_stale_jobs_failed(
    stale_after_seconds: float,
    reason: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> List[str]:
    """Mark stale pending/running jobs as failed unless they are waiting for answers."""
    return _manager(cache_dir).mark_stale_active_jobs_failed(
        stale_after_seconds=stale_after_seconds,
        reason=reason,
    )


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges with existing data and persists to cache."""
    _manager(cache_dir).update_job(job_id, **kwargs)


def update_task_state(
    job_id: str,
    task_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update state for a single task. Merges kwargs into job["task_states"][task_id]."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        task_states = data.setdefault("task_states", {})
        existing = task_states.get(task_id, {})
        task_states[task_id] = {**existing, **kwargs}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update_job_team_progress(
    job_id: str,
    team_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update progress for a single team. Merges kwargs into job["team_progress"][team_id]."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        team_progress = data.setdefault("team_progress", {})
        existing = team_progress.get(team_id, {})
        team_progress[team_id] = {**existing, **kwargs}
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


def add_pending_questions(
    job_id: str,
    questions: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Add pending questions and set waiting_for_answers=True to pause job."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        existing = data.get("pending_questions", [])
        existing.extend(questions)
        data["pending_questions"] = existing
        data["waiting_for_answers"] = True
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def submit_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store submitted answers, clear pending questions, and resume job."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        existing_answers = data.get("submitted_answers", [])
        existing_answers.extend(answers)
        data["submitted_answers"] = existing_answers
        data["pending_questions"] = []
        data["waiting_for_answers"] = False
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_waiting_for_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Check if job is waiting for user answers."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        return bool(data.get("waiting_for_answers", False))


def get_submitted_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    """Get submitted answers for a job."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        return list(data.get("submitted_answers", []))


def request_cancel(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Request cancellation for a job. Sets cancel_requested=True and status to cancelled.
    
    Returns True if the job was found and cancellation was requested.
    Returns False if the job was not found or is already in a terminal state.
    """
    terminal_statuses = (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path)
        if data is None:
            return False
        current_status = data.get("status", JOB_STATUS_PENDING)
        if current_status in terminal_statuses:
            return False
        data["cancel_requested"] = True
        data["status"] = JOB_STATUS_CANCELLED
        data["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Job %s cancellation requested", job_id)
        return True


def is_cancel_requested(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Check if cancellation has been requested for a job."""
    with _lock:
        path = _job_file(job_id, cache_dir)
        data = _read_job_file(path) or {}
        return bool(data.get("cancel_requested", False))
