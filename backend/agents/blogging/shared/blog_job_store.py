"""
Job store for blogging pipeline: persists job status and progress via the job service.

Uses ``JobServiceClient`` to communicate with the job service container over
HTTP when ``JOB_SERVICE_URL`` is set, falling back to a local
``CentralJobManager`` for non-Docker development.

Note: Jobs created before migration from the legacy store (under .agent_cache/blog_jobs/)
are not automatically migrated. New jobs use the central store only. Historical jobs
in the old path are not read by this module.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_service_client import JobServiceClient, start_stale_job_monitor

# Re-export generic checkpoint helpers so blogging-specific callers can keep
# importing from blog_job_store while new teams use ``shared_temporal`` directly.
from shared_temporal.checkpoints import (  # noqa: F401
    load_checkpoint,
    save_checkpoint,
    submit_input,
    wait_for_input,
)

logger = logging.getLogger(__name__)

# Stale job monitor: mark PENDING/running jobs with no recent heartbeat as failed
_blog_stale_monitor_stop: Optional[Any] = None


def _start_blog_stale_monitor() -> None:
    global _blog_stale_monitor_stop
    if _blog_stale_monitor_stop is not None:
        return
    try:
        _blog_stale_monitor_stop = start_stale_job_monitor(
            _client(DEFAULT_CACHE_DIR),
            interval_seconds=300.0,
            stale_after_seconds=3600.0,
            reason="Blog pipeline job heartbeat stale (pending/running too long without progress)",
        )
        logger.info("Started blog job stale monitor (stale_after=3600s)")
    except Exception as e:
        logger.warning("Could not start blog stale job monitor: %s", e)


def stop_blog_stale_monitor() -> None:
    """Signal the stale-job background thread to exit (call during API shutdown)."""
    global _blog_stale_monitor_stop
    if _blog_stale_monitor_stop is not None:
        _blog_stale_monitor_stop.set()


# Job status constants — common ones from shared client, plus blogging-specific
from job_service_client import (  # noqa: E402
    JOB_STATUS_CANCELLED,  # noqa: F401 — re-exported for run_pipeline_job
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
)

JOB_STATUS_NEEDS_REVIEW = "needs_human_review"

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache")).resolve()


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    return JobServiceClient(team="blogging_team", cache_dir=str(cache_dir))


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
    """Create a new blog job with pending status."""
    now = datetime.now(timezone.utc).isoformat()
    fields: Dict[str, Any] = {
        "brief": brief,
        "audience": audience,
        "tone_or_purpose": tone_or_purpose,
        "work_dir": work_dir,
        "job_type": job_type,
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
        "current_gap_round": 0,
        "story_chat_history": [],
        "elicited_stories": [],
        # General Q&A (mirrors SE team pattern)
        "pending_questions": [],
        "waiting_for_answers": False,
        "submitted_answers": [],
        # Interactive draft review (user-as-editor)
        "waiting_for_draft_feedback": False,
        "draft_for_review": None,
        "draft_review_revision": 0,
        "draft_review_questions": [],
        "draft_escalation_summary": None,
        "user_draft_feedback": None,
        "guideline_updates_applied": [],
        "events": [],
    }
    _client(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **fields)


def reset_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Reset a blog job to initial state for restart (preserves created_at, brief, audience, work_dir)."""
    update_blog_job(
        job_id,
        cache_dir=cache_dir,
        status=JOB_STATUS_PENDING,
        phase=None,
        progress=0,
        status_text="Restarting...",
        error=None,
        failed_phase=None,
        started_at=None,
        completed_at=None,
        title_choices=[],
        outline=None,
        draft_preview=None,
        selected_title=None,
        waiting_for_title_selection=False,
        waiting_for_story_input=False,
        waiting_for_answers=False,
        waiting_for_draft_feedback=False,
        pending_questions=[],
        submitted_answers=[],
        draft_for_review=None,
        draft_review_revision=0,
        user_draft_feedback=None,
        draft_escalation_summary=None,
        story_gaps=[],
        current_story_gap_index=0,
        story_chat_history=[],
        elicited_stories=[],
        draft_iterations=0,
        rewrite_iterations=0,
    )


def get_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data, or None if not found."""
    return _client(cache_dir).get_job(job_id)


def list_blog_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
) -> List[Dict[str, Any]]:
    """List blog jobs. If running_only is True, only include pending or running."""
    statuses: Optional[List[str]] = (
        [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    )
    raw = _client(cache_dir).list_jobs(statuses=statuses)
    result: List[Dict[str, Any]] = []
    for data in raw:
        result.append(
            {
                "job_id": data.get("job_id", ""),
                "status": data.get("status", JOB_STATUS_PENDING),
                "brief": (data.get("brief") or "")[:100],
                "phase": data.get("phase"),
                "progress": data.get("progress", 0),
                "created_at": data.get("created_at"),
                "job_type": data.get("job_type"),
            }
        )
    return result


def update_blog_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges with existing data."""
    _client(cache_dir).update_job(job_id, **kwargs)


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
        "status_text": "Pipeline complete"
        if status == JOB_STATUS_COMPLETED
        else "Needs human review",
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
    """Mark all pending or running blog jobs as interrupted (e.g. on server shutdown)."""
    try:
        _client(cache_dir).mark_all_active_jobs_interrupted(reason)
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
    """Delete a job from the store. Returns True if deleted, False if not found."""
    return _client(cache_dir).delete_job(job_id)


# ---------------------------------------------------------------------------
# Title selection pause/resume
# ---------------------------------------------------------------------------


def submit_title_selection(
    job_id: str,
    title: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store the selected title and resume the pipeline (clears waiting_for_title_selection)."""
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"selected_title": title, "waiting_for_title_selection": False},
    )


def submit_title_ratings(
    job_id: str,
    ratings: list[dict],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store title ratings and resume the pipeline so it can generate new titles.

    Each rating is ``{"title": str, "rating": "dislike"|"like"|"love"}``.
    If any title is rated "love", it becomes the selected title and the pipeline proceeds.
    Otherwise the rating is queued as pending feedback — the pipeline polling loop picks it
    up, generates a replacement title for the rated one, and keeps waiting for more input.
    """
    loved = [r for r in ratings if r.get("rating") == "love"]
    if loved:
        _client(cache_dir).atomic_update(
            job_id,
            merge_fields={
                "selected_title": loved[0]["title"],
                "waiting_for_title_selection": False,
                "title_ratings": ratings,
            },
        )
    else:
        # Keep waiting_for_title_selection True so the UI stays visible.
        # Store the rating in pending_title_feedback for the pipeline to pick up.
        _client(cache_dir).atomic_update(
            job_id,
            merge_fields={
                "pending_title_feedback": ratings,
            },
        )


def is_waiting_for_title_selection(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the pipeline is paused waiting for a title selection."""
    job = _client(cache_dir).get_job(job_id)
    return bool(job.get("waiting_for_title_selection")) if job else False


def get_pending_title_feedback(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> list[dict] | None:
    """Return pending title feedback ratings if any, otherwise None."""
    job = _client(cache_dir).get_job(job_id)
    if not job:
        return None
    return job.get("pending_title_feedback") or None


def clear_pending_title_feedback(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Clear the pending title feedback field after processing."""
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"pending_title_feedback": None},
    )


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
    # Read current gap_round so the message can be tagged for filtering
    job = _client(cache_dir).get_job(job_id)
    gap_round = (job or {}).get("current_gap_round", 0)
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"waiting_for_story_input": True},
        append_to={
            "story_chat_history": [
                {"role": "agent", "content": content, "gap_index": gap_index, "gap_round": gap_round}
            ]
        },
    )


def submit_story_user_message(
    job_id: str,
    message: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Append a user message to the story chat history and resume the pipeline."""
    # Read current gap_round so the message can be tagged for filtering
    job = _client(cache_dir).get_job(job_id)
    gap_round = (job or {}).get("current_gap_round", 0)
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"waiting_for_story_input": False},
        append_to={
            "story_chat_history": [
                {"role": "user", "content": message, "gap_index": 0, "gap_round": gap_round}
            ]
        },
    )


def skip_current_story_gap(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Skip the current story gap and advance to the next one (clears waiting flag).

    Uses an atomic increment so concurrent skip requests cannot lose an update.
    """
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"waiting_for_story_input": False},
        increment={"current_story_gap_index": 1},
    )


def complete_story_elicitation(
    job_id: str,
    elicited_stories: List[str],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store compiled elicited story narratives; clears all story-wait flags."""
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"elicited_stories": elicited_stories, "waiting_for_story_input": False},
    )


def is_waiting_for_story_input(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the pipeline is paused waiting for a story message."""
    job = _client(cache_dir).get_job(job_id)
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
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"pending_questions": questions, "waiting_for_answers": True},
    )


def submit_blog_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store submitted answers, clear pending questions, and resume the pipeline."""
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"pending_questions": [], "waiting_for_answers": False},
        append_to={"submitted_answers": answers},
    )


def is_waiting_for_blog_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the pipeline is paused waiting for Q&A answers."""
    job = _client(cache_dir).get_job(job_id)
    return bool(job.get("waiting_for_answers")) if job else False


# ---------------------------------------------------------------------------
# Interactive draft review pause/resume (user-as-editor)
# ---------------------------------------------------------------------------


def request_draft_feedback(
    job_id: str,
    draft: str,
    revision: int,
    *,
    uncertainty_questions: Optional[List[Dict[str, Any]]] = None,
    escalation_summary: Optional[str] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Pause the pipeline and present a draft to the user for feedback.

    Sets ``waiting_for_draft_feedback=True`` so the pipeline blocks until the
    user submits feedback or approves the draft via ``submit_draft_feedback``.
    """
    fields: Dict[str, Any] = {
        "waiting_for_draft_feedback": True,
        "draft_for_review": draft,
        "draft_review_revision": revision,
        "user_draft_feedback": None,
    }
    if uncertainty_questions is not None:
        fields["draft_review_questions"] = uncertainty_questions
    if escalation_summary is not None:
        fields["draft_escalation_summary"] = escalation_summary
    _client(cache_dir).atomic_update(job_id, merge_fields=fields)


def submit_draft_feedback(
    job_id: str,
    feedback: str,
    approved: bool,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store user feedback on the draft and resume the pipeline.

    Args:
        feedback: Free-form feedback text from the user.
        approved: True if the user approves the draft as-is.
    """
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={
            "waiting_for_draft_feedback": False,
            "user_draft_feedback": {"feedback": feedback, "approved": approved},
            "draft_review_questions": [],
            "draft_escalation_summary": None,
        },
    )


def is_waiting_for_draft_feedback(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Return True if the pipeline is paused waiting for user feedback on a draft."""
    job = _client(cache_dir).get_job(job_id)
    return bool(job.get("waiting_for_draft_feedback")) if job else False


def get_user_draft_feedback(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Retrieve the latest user draft feedback (or None if not yet submitted)."""
    job = _client(cache_dir).get_job(job_id)
    return job.get("user_draft_feedback") if job else None


def record_guideline_updates(
    job_id: str,
    updates: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Append applied guideline updates to the job record for audit trail."""
    _client(cache_dir).atomic_update(
        job_id,
        append_to={"guideline_updates_applied": updates},
    )


# Start stale job monitor when module is loaded (e.g. when blogging API is mounted)
_start_blog_stale_monitor()
