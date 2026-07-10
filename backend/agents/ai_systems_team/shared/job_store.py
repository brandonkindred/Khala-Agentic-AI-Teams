"""
Job store for AI Systems team: persists async job status via the job service.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from job_service_client import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
    get_job_service_client,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR: Path = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))


def _client(cache_dir: Path | str = DEFAULT_CACHE_DIR) -> JobServiceClient:
    """Return the cached JobServiceClient for the ai_systems_team.

    cache_dir is accepted for API compatibility with callers written when this
    module was file-backed. JobServiceClient uses HTTP (configured via
    JOB_SERVICE_URL) and does not use a local filesystem cache, so cache_dir is
    intentionally not forwarded.
    """
    return get_job_service_client("ai_systems_team")


def create_job(
    job_id: str,
    project_name: str,
    spec_path: str,
    constraints: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Create a new AI system build job with initial state."""
    now = datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = {
        "project_name": project_name,
        "spec_path": spec_path,
        "constraints": constraints or {},
        "output_dir": output_dir,
        "progress": 0,
        "current_phase": None,
        "completed_phases": [],
        "phase_results": {},
        "blueprint": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "events": [],
    }
    _client(cache_dir).create_job(job_id, status=JOB_STATUS_PENDING, **data)


def get_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Dict[str, Any]:
    """Get job data by ID. Returns empty dict if not found."""
    return _client(cache_dir).get_job(job_id) or {}


def update_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    **kwargs: Any,
) -> None:
    """Update job fields. Merges kwargs into existing job data."""
    _client(cache_dir).update_job(job_id, **kwargs)


def make_job_updater(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Callable[..., None]:
    """Build the progress callback the phase functions call during a run.

    A single source of truth for the ``job_updater`` shape shared by thread mode
    (``api.main._run_build_background``) and every Temporal phase activity, so the
    two runtimes write identical job-store progress fields.

    Preconditions:
        - ``job_id`` identifies a created job record (updates on a missing job are
          a no-op at the client layer).
    Postconditions:
        - Returns a callable ``job_updater(current_phase=?, progress=?,
          status_text=?, blueprint_snapshot=?)`` that writes only the supplied
          fields (``blueprint_snapshot`` maps to the job's ``blueprint`` field) and
          is a no-op when called with no arguments.
    """

    def job_updater(
        current_phase: Optional[str] = None,
        progress: Optional[int] = None,
        status_text: Optional[str] = None,
        blueprint_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        updates: Dict[str, Any] = {}
        if current_phase is not None:
            updates["current_phase"] = current_phase
        if progress is not None:
            updates["progress"] = progress
        if status_text is not None:
            updates["status_text"] = status_text
        if blueprint_snapshot is not None:
            updates["blueprint"] = blueprint_snapshot
        if updates:
            update_job(job_id, cache_dir=cache_dir, **updates)

    return job_updater


def record_phase_result(
    job_id: str,
    phase_name: str,
    result: Dict[str, Any],
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Merge a completed phase's result into the job's stored blueprint snapshot.

    This is the Temporal per-phase checkpoint: after each phase activity succeeds
    it records its serialized result here so (a) a resumed workflow can skip the
    phase and reuse the result, and (b) the job-store ``blueprint`` field mirrors
    the incremental blueprint the thread-mode orchestrator writes via its
    ``_checkpoint`` callback.

    Preconditions:
        - ``phase_name`` is a valid ``Phase`` value.
        - ``result`` is the phase model serialized with ``model_dump(mode="json")``.
    Postconditions:
        - No-op when the job does not exist. Otherwise the stored blueprint gains
          ``result`` under ``phase_name``, ``phase_name`` is appended to
          ``completed_phases`` (idempotently), and ``current_phase`` is set to it.
          The write is schema-validated through ``AgentBlueprint`` so a malformed
          result surfaces here rather than at status-read time.
    """
    from ..models import AgentBlueprint

    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return

    stored = data.get("blueprint")
    if isinstance(stored, dict):
        bp_dict: Dict[str, Any] = dict(stored)
    else:
        bp_dict = {
            "project_name": data.get("project_name") or "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    bp_dict[phase_name] = result
    bp_dict["current_phase"] = phase_name
    completed = list(bp_dict.get("completed_phases") or [])
    if phase_name not in completed:
        completed.append(phase_name)
    bp_dict["completed_phases"] = completed

    # Validate/normalize through the model so the persisted snapshot always matches
    # AgentBlueprint's schema (version default, enum coercion for phase strings).
    blueprint = AgentBlueprint(**bp_dict)
    update_job(job_id, cache_dir=cache_dir, blueprint=blueprint.model_dump(mode="json"))


def list_jobs(
    running_only: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> List[Dict[str, Any]]:
    """List all jobs, optionally filtered to running/pending only."""
    statuses: Optional[List[str]] = (
        [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    )
    return _client(cache_dir).list_jobs(statuses=statuses)


def mark_all_running_jobs_failed(
    reason: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark all pending or running AI systems jobs as failed (e.g. on server shutdown)."""
    try:
        _client(cache_dir).mark_all_active_jobs_failed(reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def cancel_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Set job status to cancelled. Returns True if job existed and was updated."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return False
    _client(cache_dir).update_job(job_id, status=JOB_STATUS_CANCELLED, heartbeat=False)
    return True


def mark_job_running(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as running."""
    update_job(job_id, cache_dir, status=JOB_STATUS_RUNNING)


def mark_job_completed(
    job_id: str,
    blueprint: Optional[Dict[str, Any]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as completed with optional blueprint."""
    updates: Dict[str, Any] = {"status": JOB_STATUS_COMPLETED, "progress": 100}
    if blueprint is not None:
        updates["blueprint"] = blueprint
    update_job(job_id, cache_dir=cache_dir, **updates)


def mark_job_failed(
    job_id: str,
    error: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Mark a job as failed with error message."""
    update_job(job_id, cache_dir=cache_dir, status=JOB_STATUS_FAILED, error=error)


def update_phase_progress(
    job_id: str,
    current_phase: str,
    progress: int,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Update job with current phase progress."""
    update_job(
        job_id,
        cache_dir=cache_dir,
        current_phase=current_phase,
        progress=progress,
    )


def add_completed_phase(
    job_id: str,
    phase: str,
    phase_result: Optional[Dict[str, Any]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Add a phase to the completed phases list."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return
    completed = list(data.get("completed_phases", []))
    if phase not in completed:
        completed.append(phase)
    updates: Dict[str, Any] = {"completed_phases": completed}
    if phase_result is not None:
        phase_results = dict(data.get("phase_results", {}))
        phase_results[phase] = phase_result
        updates["phase_results"] = phase_results
    update_job(job_id, cache_dir=cache_dir, **updates)


def reset_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    """Reset a job to its initial state for restart (preserves created_at and input params)."""
    data = get_job(job_id, cache_dir=cache_dir)
    if not data:
        return
    update_job(
        job_id,
        cache_dir=cache_dir,
        status=JOB_STATUS_PENDING,
        progress=0,
        current_phase=None,
        completed_phases=[],
        phase_results={},
        blueprint=None,
        error=None,
        status_text=None,
    )


def delete_job(
    job_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Delete a job. Returns True if deleted, False if not found."""
    return _client(cache_dir).delete_job(job_id)
