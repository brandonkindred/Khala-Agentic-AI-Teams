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
    return JobServiceClient(team="coding_team", cache_dir=str(cache_dir))


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
