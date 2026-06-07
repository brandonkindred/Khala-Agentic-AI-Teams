"""
Job store for coding_team: persists job status and task graph snapshot via the job service.
Used for status API and resume; task graph snapshot and agent_task_map are stored on the job.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_service_client import JobServiceClient

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR: Path = Path(os.getenv("AGENT_CACHE", ".agent_cache"))


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    return JobServiceClient(team="coding_team")


def create_job(
    job_id: str,
    repo_path: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    plan_input: Optional[Dict[str, Any]] = None,
) -> None:
    """Create a new coding_team job with pending status."""
    data: Dict[str, Any] = {
        "repo_path": repo_path,
        "phase": "task_graph",
        "status_text": "",
        "progress": 0,
        "task_graph_snapshot": [],
        "agent_task_map": {},
        "stack_specs": [],
        "error": None,
        "plan_input": plan_input or {},
        "events": [],
        # Human-in-the-loop gate state (mirrors the SE job-record contract so the same answer
        # protocol resumes a pause on either path).
        "pending_questions": [],
        "waiting_for_answers": False,
        "submitted_answers": [],
    }
    _client(cache_dir).create_job(job_id, status="pending", **data)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data. Returns None if not found."""
    return _client(cache_dir).get_job(job_id)


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    heartbeat: bool = True,
    **fields: Any,
) -> None:
    """Update job with given fields (e.g. status, phase, status_text, task_graph_snapshot, agent_task_map)."""
    _client(cache_dir).update_job(job_id, heartbeat=heartbeat, **fields)


def update_job_task_graph(
    job_id: str,
    task_graph_snapshot: Dict[str, Any],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Persist task graph snapshot and agent_task_map to the job (for status API and resume)."""
    update_job(
        job_id,
        cache_dir=cache_dir,
        heartbeat=True,
        task_graph_snapshot=task_graph_snapshot.get("tasks", []),
        agent_task_map=task_graph_snapshot.get("agent_task_map", {}),
    )


def list_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    running_only: bool = False,
) -> List[Dict[str, Any]]:
    """List coding_team jobs. If running_only, only pending or running."""
    statuses = ["pending", "running"] if running_only else None
    return _client(cache_dir).list_jobs(statuses=statuses)


# ---------------------------------------------------------------------------
# Human-in-the-loop pause / answer operations
#
# These mirror software_engineering_team/shared/job_store.py so the coding team's pause uses the
# same job-record fields (waiting_for_answers / pending_questions / submitted_answers) as the SE
# gate. Writes are atomic via the job service so a concurrent answer submission and a status update
# cannot clobber each other.
# ---------------------------------------------------------------------------


def add_pending_questions(
    job_id: str,
    questions: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Append pending questions and set waiting_for_answers=True to pause the job.

    Preconditions:
        - ``questions`` is a list of structured question dicts (id, question_text, options, ...).
    Postconditions:
        - The job's ``waiting_for_answers`` is True and ``questions`` are appended to
          ``pending_questions``.
    """
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"waiting_for_answers": True},
        append_to={"pending_questions": questions},
    )


def is_waiting_for_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> bool:
    """True iff the job is currently paused waiting for user answers."""
    data = _client(cache_dir).get_job(job_id)
    return bool(data.get("waiting_for_answers", False)) if data else False


def submit_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Store submitted answers, clear pending questions, and clear the waiting flag to resume.

    Postconditions:
        - ``waiting_for_answers`` is False, ``pending_questions`` is empty, and ``answers`` are
          appended to ``submitted_answers``. The orchestrator's wait loop resumes on the cleared
          flag and reads ``submitted_answers``.
    """
    _client(cache_dir).atomic_update(
        job_id,
        merge_fields={"pending_questions": [], "waiting_for_answers": False},
        append_to={"submitted_answers": answers},
    )


def get_submitted_answers(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    """Return the answers submitted for this job (empty when none/unknown)."""
    data = _client(cache_dir).get_job(job_id)
    return list(data.get("submitted_answers") or []) if data else []
