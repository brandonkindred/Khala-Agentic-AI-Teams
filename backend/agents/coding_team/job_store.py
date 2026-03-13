"""
Job store for coding_team: persists job status and task graph snapshot via CentralJobManager.
Used for status API and resume; task graph snapshot and agent_task_map are stored on the job.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR: Path = Path(os.getenv("AGENT_CACHE", ".agent_cache")).resolve()


def _manager(cache_dir: str | Path = DEFAULT_CACHE_DIR):
    from shared_job_management import CentralJobManager
    return CentralJobManager(team="coding_team", cache_dir=cache_dir)


def create_job(
    job_id: str,
    repo_path: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    plan_input: Optional[Dict[str, Any]] = None,
) -> None:
    """Create a new coding_team job with pending status."""
    data: Dict[str, Any] = {
        "job_id": job_id,
        "repo_path": repo_path,
        "status": "pending",
        "phase": "task_graph",
        "status_text": "",
        "progress": 0,
        "task_graph_snapshot": [],
        "agent_task_map": {},
        "stack_specs": [],
        "error": None,
        "plan_input": plan_input or {},
    }
    _manager(cache_dir).create_job(**data)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data. Returns None if not found."""
    data = _manager(cache_dir).get_job(job_id)
    return copy.deepcopy(data) if data else None


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    heartbeat: bool = True,
    **fields: Any,
) -> None:
    """Update job with given fields (e.g. status, phase, status_text, task_graph_snapshot, agent_task_map)."""
    _manager(cache_dir).update_job(job_id, heartbeat=heartbeat, **fields)


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
    jobs = _manager(cache_dir).list_jobs(statuses=statuses)
    return [copy.deepcopy(j) for j in jobs]
