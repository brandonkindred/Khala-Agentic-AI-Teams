"""
Adapter to call the AI Systems Team for building a new agent system.

Calls the AI Systems Team API:
- POST /api/ai-systems/build -> AISystemRequest { project_name, spec_path, constraints?, output_dir? } -> { job_id }
- GET  /api/ai-systems/build/status/{job_id} -> status, blueprint (when completed), current_phase, progress, error
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from planning_team.adapters._base import BaseAdapter
from shared.http.job_polling import get_json, poll_until_terminal, post_json

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
POLL_INTERVAL = 5.0
MAX_POLL_WAIT = 3600.0

_TERMINAL_STATUSES = frozenset({"completed", "failed"})

_adapter = BaseAdapter(
    env_var="PLANNING_AI_SYSTEMS_URL",
    path_prefix="/api/ai-systems",
    unconfigured_log="AI Systems build",
)


def start_ai_systems_build(
    project_name: str,
    spec_path: str,
    constraints: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
) -> Optional[str]:
    """
    Start an AI Systems build job. spec_path must be a path to a spec file on disk
    (AI Systems API expects a file path). Returns job_id or None on failure
    (including when the AI Systems service is unconfigured).
    """
    url = _adapter.build_url("/build")
    if not url:
        return None
    payload: Dict[str, Any] = {
        "project_name": project_name,
        "spec_path": spec_path,
        "constraints": constraints or {},
    }
    if output_dir is not None:
        payload["output_dir"] = output_dir
    data = post_json(url, payload, timeout=DEFAULT_TIMEOUT, log_context="AI Systems build start")
    return data.get("job_id") if data else None


def get_ai_systems_build_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get status of an AI Systems build job. Returns None on failure."""
    url = _adapter.build_url(f"/build/status/{job_id}")
    if not url:
        return None
    return get_json(
        url, timeout=DEFAULT_TIMEOUT, log_context=f"AI Systems build status for {job_id}"
    )


def wait_for_ai_systems_build_completion(
    job_id: str,
    poll_interval: float = POLL_INTERVAL,
    max_wait: float = MAX_POLL_WAIT,
) -> Dict[str, Any]:
    """
    Poll until build is completed or failed. Returns dict with status and optional
    blueprint (when completed).
    """
    return poll_until_terminal(
        lambda: get_ai_systems_build_status(job_id),
        terminal_statuses=_TERMINAL_STATUSES,
        poll_interval=poll_interval,
        total_timeout=max_wait,
        log_context="AI Systems build",
    )
