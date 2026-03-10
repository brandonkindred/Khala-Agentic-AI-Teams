"""
Job store for async API: persists job status and progress per job in a cache directory.

Uses AGENT_CACHE environment variable when set (otherwise .agent_cache), resolved to an
absolute path so list/create use the same directory regardless of process CWD. Each job
is stored under {cache_dir}/software_engineering_team/jobs/{job_id}.json via
CentralJobManager so state survives process restarts.

Stale jobs: JOB_STALE_AFTER_SECONDS (env JOB_STALE_AFTER_SECONDS, default 1800) is the
age in seconds after which a pending/running job with no recent heartbeat is marked failed.
"""

from __future__ import annotations

import copy
import logging
import os
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

# Seconds after which a pending/running job with no recent heartbeat is marked failed.
# Set via env JOB_STALE_AFTER_SECONDS (default 1800).
def get_stale_after_seconds() -> float:
    try:
        return float(os.getenv("JOB_STALE_AFTER_SECONDS", "1800"))
    except (TypeError, ValueError):
        return 1800.0


_jobs_path_logged = False


def _manager(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> CentralJobManager:
    global _jobs_path_logged
    if not _jobs_path_logged:
        jobs_dir = Path(cache_dir) / "software_engineering_team" / "jobs"
        logger.info("Software engineering job store path: %s", jobs_dir)
        _jobs_path_logged = True
    return CentralJobManager(team="software_engineering_team", cache_dir=cache_dir)


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


def reset_job(
    job_id: str,
    repo_path: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    job_type: Optional[str] = None,
) -> None:
    """Reset an existing job to initial state (same job_id). Preserves created_at so job order is unchanged."""
    existing = get_job(job_id, cache_dir)
    created_at = (
        existing.get("created_at")
        if existing
        else datetime.now(timezone.utc).isoformat()
    )
    now = datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = {
        "job_id": job_id,
        "team": "software_engineering_team",
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
        "created_at": created_at,
        "updated_at": now,
        "last_heartbeat_at": now,
        "events": [],
    }
    if job_type is not None:
        data["job_type"] = job_type
    _manager(cache_dir).replace_job(job_id, data)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data from cache, or None if not found."""
    data = _manager(cache_dir).get_job(job_id)
    return copy.deepcopy(data) if data else None


def delete_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Remove the job from the store. Returns True if removed, False if not found."""
    return _manager(cache_dir).delete_job(job_id)


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


def start_job_heartbeat_thread(
    job_id: str,
    interval_seconds: float = 120.0,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Start a daemon thread that periodically updates the job's heartbeat (last_heartbeat_at)
    while the job is pending or running. The thread exits when the job is missing or in a
    terminal status (completed, failed, cancelled, etc.)."""
    active_statuses = (JOB_STATUS_PENDING, JOB_STATUS_RUNNING)

    def _heartbeat_loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                data = get_job(job_id, cache_dir=cache_dir)
                if not data:
                    return
                if data.get("status") not in active_statuses:
                    return
                update_job(job_id, cache_dir=cache_dir)
            except Exception as exc:
                logger.warning("Job heartbeat thread for %s: %s", job_id, exc)
                # Continue loop so one failure does not kill the thread

    thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"job-heartbeat-{job_id[:8]}",
        daemon=True,
    )
    thread.start()


def update_task_state(
    job_id: str,
    task_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update state for a single task. Merges kwargs into job["task_states"][task_id]."""
    def merge(data: Dict[str, Any]) -> None:
        task_states = data.setdefault("task_states", {})
        existing = task_states.get(task_id, {})
        task_states[task_id] = {**existing, **kwargs}
    _manager(cache_dir).apply_to_job(job_id, merge)


def update_job_team_progress(
    job_id: str,
    team_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update progress for a single team. Merges kwargs into job["team_progress"][team_id]."""
    def merge(data: Dict[str, Any]) -> None:
        team_progress = data.setdefault("team_progress", {})
        existing = team_progress.get(team_id, {})
        team_progress[team_id] = {**existing, **kwargs}
    _manager(cache_dir).apply_to_job(job_id, merge)


def add_task_result(
    job_id: str,
    result: Dict[str, Any],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Append a task result to the job and persist to cache."""
    def append(data: Dict[str, Any]) -> None:
        results = data.get("task_results", [])
        results.append(result)
        data["task_results"] = results
    _manager(cache_dir).apply_to_job(job_id, append)


def add_pending_questions(
    job_id: str,
    questions: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Add pending questions and set waiting_for_answers=True to pause job."""
    def add(data: Dict[str, Any]) -> None:
        existing = data.get("pending_questions", [])
        existing.extend(questions)
        data["pending_questions"] = existing
        data["waiting_for_answers"] = True
    _manager(cache_dir).apply_to_job(job_id, add)


def submit_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store submitted answers, clear pending questions, and resume job."""
    def apply(data: Dict[str, Any]) -> None:
        existing_answers = data.get("submitted_answers", [])
        existing_answers.extend(answers)
        data["submitted_answers"] = existing_answers
        data["pending_questions"] = []
        data["waiting_for_answers"] = False
    _manager(cache_dir).apply_to_job(job_id, apply)


def is_waiting_for_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Check if job is waiting for user answers."""
    data = _manager(cache_dir).get_job(job_id)
    return bool(data.get("waiting_for_answers", False)) if data else False


def get_submitted_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    """Get submitted answers for a job."""
    data = _manager(cache_dir).get_job(job_id)
    return list(data.get("submitted_answers", [])) if data else []


def request_cancel(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Request cancellation for a job. Sets cancel_requested=True and status to cancelled.

    Returns True if the job was found and cancellation was requested.
    Returns False if the job was not found or is already in a terminal state.
    """
    terminal_statuses = (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)
    data = _manager(cache_dir).get_job(job_id)
    if data is None:
        return False
    current_status = data.get("status", JOB_STATUS_PENDING)
    if current_status in terminal_statuses:
        return False

    def set_cancelled(d: Dict[str, Any]) -> None:
        d["cancel_requested"] = True
        d["status"] = JOB_STATUS_CANCELLED
        d["cancelled_at"] = datetime.now(timezone.utc).isoformat()

    _manager(cache_dir).apply_to_job(job_id, set_cancelled)
    logger.info("Job %s cancellation requested", job_id)
    return True


def is_cancel_requested(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Check if cancellation has been requested for a job."""
    data = _manager(cache_dir).get_job(job_id)
    return bool(data.get("cancel_requested", False)) if data else False
