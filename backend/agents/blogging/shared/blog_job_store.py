"""
Job store for blogging pipeline: persists job status and progress via CentralJobManager.

Jobs are stored under {cache_dir}/blogging_team/jobs/{job_id}.json so state survives
process restarts. This enables async API endpoints with polling for UI progress tracking.

Note: Jobs created before migration from the legacy store (under .agent_cache/blog_jobs/)
are not automatically migrated. New jobs use the central store only. Historical jobs
in the old path are not read by this module.
"""

from __future__ import annotations

import copy
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared_job_management import CentralJobManager, start_stale_job_monitor

logger = logging.getLogger(__name__)

# Stale job monitor: mark PENDING/running jobs with no recent heartbeat as failed
_blog_stale_monitor_stop: Optional[Any] = None


def _start_blog_stale_monitor() -> None:
    global _blog_stale_monitor_stop
    if _blog_stale_monitor_stop is not None:
        return
    try:
        _blog_stale_monitor_stop = start_stale_job_monitor(
            _manager(DEFAULT_CACHE_DIR),
            interval_seconds=300.0,
            stale_after_seconds=3600.0,
            reason="Blog pipeline job heartbeat stale (pending/running too long without progress)",
        )
        logger.info("Started blog job stale monitor (stale_after=3600s)")
    except Exception as e:
        logger.warning("Could not start blog stale job monitor: %s", e)

# Job status constants
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_NEEDS_REVIEW = "needs_human_review"

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache")).resolve()


def _manager(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> CentralJobManager:
    return CentralJobManager(team="blogging_team", cache_dir=cache_dir)


def medium_stats_run_dir(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Path:
    """Resolved directory for Medium stats job artifacts (creates parents and the job folder)."""
    cache_path = Path(cache_dir).resolve()
    custom = os.environ.get("BLOGGING_MEDIUM_STATS_ROOT")
    if custom:
        base = Path(custom).expanduser().resolve()
    else:
        base = cache_path / "blogging_team" / "medium_stats_runs"
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def create_blog_job(
    job_id: str,
    brief: str,
    *,
    audience: Optional[str] = None,
    tone_or_purpose: Optional[str] = None,
    work_dir: Optional[str] = None,
    job_type: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Create a new blog job with pending status and persist to cache."""
    now = datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = {
        "job_id": job_id,
        "brief": brief,
        "audience": audience,
        "tone_or_purpose": tone_or_purpose,
        "work_dir": work_dir,
        "job_type": job_type,
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
        "created_at": now,
        "started_at": None,
        "completed_at": None,
        # Title selection
        "waiting_for_title_selection": False,
        "selected_title": None,
        # Story elicitation
        "waiting_for_story_input": False,
        "story_gaps": [],
        "current_story_gap_index": 0,
        "story_chat_history": [],
        "elicited_stories": [],
        # General Q&A (mirrors SE team pattern)
        "pending_questions": [],
        "waiting_for_answers": False,
        "submitted_answers": [],
    }
    # create_job(job_id, **fields) expects job_id as first arg; omit job_id from **data to avoid duplicate
    fields = {k: v for k, v in data.items() if k != "job_id"}
    _manager(cache_dir).create_job(job_id, **fields)


def get_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data from cache, or None if not found."""
    data = _manager(cache_dir).get_job(job_id)
    return copy.deepcopy(data) if data else None


def list_blog_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
) -> List[Dict[str, Any]]:
    """List blog jobs from cache. If running_only is True, only include pending or running."""
    statuses: Optional[List[str]] = (
        [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    )
    raw = _manager(cache_dir).list_jobs(statuses=statuses)
    result: List[Dict[str, Any]] = []
    for data in raw:
        result.append({
            "job_id": data.get("job_id", ""),
            "status": data.get("status", JOB_STATUS_PENDING),
            "brief": (data.get("brief") or "")[:100],
            "phase": data.get("phase"),
            "progress": data.get("progress", 0),
            "created_at": data.get("created_at"),
            "job_type": data.get("job_type"),
        })
    return result


def update_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges with existing data and persists to cache."""
    _manager(cache_dir).update_job(job_id, **kwargs)


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
    content_plan_summary: Optional[str] = None,
    planning_iterations_used: Optional[int] = None,
    parse_retry_count: Optional[int] = None,
    planning_wall_ms_total: Optional[float] = None,
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
    if content_plan_summary is not None:
        kwargs["content_plan_summary"] = content_plan_summary
    if planning_iterations_used is not None:
        kwargs["planning_iterations_used"] = planning_iterations_used
    if parse_retry_count is not None:
        kwargs["parse_retry_count"] = parse_retry_count
    if planning_wall_ms_total is not None:
        kwargs["planning_wall_ms_total"] = planning_wall_ms_total
    update_blog_job(job_id, cache_dir=cache_dir, **kwargs)


def fail_blog_job(
    job_id: str,
    error: str,
    *,
    failed_phase: Optional[str] = None,
    planning_failure_reason: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as failed with error details."""
    kwargs: Dict[str, Any] = {
        "status": JOB_STATUS_FAILED,
        "error": error,
        "failed_phase": failed_phase,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if planning_failure_reason is not None:
        kwargs["planning_failure_reason"] = planning_failure_reason
    update_blog_job(job_id, cache_dir=cache_dir, **kwargs)


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running blog jobs as failed (e.g. on server shutdown)."""
    try:
        jobs = list_blog_jobs(cache_dir=cache_dir, running_only=True)
        for job in jobs:
            job_id = job.get("job_id")
            if job_id:
                fail_blog_job(job_id, error=reason, cache_dir=cache_dir)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def approve_blog_job(
    job_id: str,
    *,
    approved_at: Optional[str] = None,
    approved_by: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as approved. Sets approved_at to now (ISO) if not provided."""
    if approved_at is None:
        approved_at = datetime.now(timezone.utc).isoformat()
    update_blog_job(
        job_id,
        cache_dir=cache_dir,
        approved_at=approved_at,
        approved_by=approved_by,
    )


def unapprove_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Clear approval for a job (set approved_at and approved_by to None)."""
    update_blog_job(
        job_id,
        cache_dir=cache_dir,
        approved_at=None,
        approved_by=None,
    )


def delete_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Delete a job from the cache. Returns True if deleted, False if not found."""
    return _manager(cache_dir).delete_job(job_id)


# ---------------------------------------------------------------------------
# Title selection pause/resume
# ---------------------------------------------------------------------------


def submit_title_selection(
    job_id: str,
    title: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store the selected title and resume the pipeline (clears waiting_for_title_selection)."""
    def apply(data: Dict[str, Any]) -> None:
        data["selected_title"] = title
        data["waiting_for_title_selection"] = False

    _manager(cache_dir).apply_to_job(job_id, apply)


def is_waiting_for_title_selection(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the pipeline is paused waiting for a title selection."""
    job = _manager(cache_dir).get_job(job_id)
    return bool(job.get("waiting_for_title_selection")) if job else False


# ---------------------------------------------------------------------------
# Story elicitation pause/resume
# ---------------------------------------------------------------------------


def add_story_agent_message(
    job_id: str,
    content: str,
    gap_index: int,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Append a ghost-writer agent message to the story chat history and pause for user input."""
    def apply(data: Dict[str, Any]) -> None:
        history: List[Dict[str, Any]] = data.get("story_chat_history", [])
        history.append({"role": "agent", "content": content, "gap_index": gap_index})
        data["story_chat_history"] = history
        data["waiting_for_story_input"] = True

    _manager(cache_dir).apply_to_job(job_id, apply)


def submit_story_user_message(
    job_id: str,
    message: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Append a user message to the story chat history and resume the pipeline."""
    def apply(data: Dict[str, Any]) -> None:
        history: List[Dict[str, Any]] = data.get("story_chat_history", [])
        history.append({"role": "user", "content": message})
        data["story_chat_history"] = history
        data["waiting_for_story_input"] = False

    _manager(cache_dir).apply_to_job(job_id, apply)


def skip_current_story_gap(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Skip the current story gap and advance to the next one (clears waiting flag)."""
    def apply(data: Dict[str, Any]) -> None:
        data["current_story_gap_index"] = data.get("current_story_gap_index", 0) + 1
        data["waiting_for_story_input"] = False

    _manager(cache_dir).apply_to_job(job_id, apply)


def complete_story_elicitation(
    job_id: str,
    elicited_stories: List[str],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store compiled elicited story narratives; clears all story-wait flags."""
    def apply(data: Dict[str, Any]) -> None:
        data["elicited_stories"] = elicited_stories
        data["waiting_for_story_input"] = False

    _manager(cache_dir).apply_to_job(job_id, apply)


def is_waiting_for_story_input(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the pipeline is paused waiting for a story message."""
    job = _manager(cache_dir).get_job(job_id)
    return bool(job.get("waiting_for_story_input")) if job else False


# ---------------------------------------------------------------------------
# General Q&A pause/resume (mirrors SE team pattern)
# ---------------------------------------------------------------------------


def add_blog_pending_questions(
    job_id: str,
    questions: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store pending questions and pause the pipeline for answers."""
    def apply(data: Dict[str, Any]) -> None:
        data["pending_questions"] = questions
        data["waiting_for_answers"] = True

    _manager(cache_dir).apply_to_job(job_id, apply)


def submit_blog_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store submitted answers, clear pending questions, and resume the pipeline."""
    def apply(data: Dict[str, Any]) -> None:
        existing: List[Dict[str, Any]] = data.get("submitted_answers", [])
        existing.extend(answers)
        data["submitted_answers"] = existing
        data["pending_questions"] = []
        data["waiting_for_answers"] = False

    _manager(cache_dir).apply_to_job(job_id, apply)


def is_waiting_for_blog_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the pipeline is paused waiting for Q&A answers."""
    job = _manager(cache_dir).get_job(job_id)
    return bool(job.get("waiting_for_answers")) if job else False


# Start stale job monitor when module is loaded (e.g. when blogging API is mounted)
_start_blog_stale_monitor()
