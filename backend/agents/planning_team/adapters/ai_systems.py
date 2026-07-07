"""
Adapter to call the AI Systems Team for building a new agent system.

Verified paths (ai_systems_team.api.main):
- POST /api/ai-systems/build -> AISystemRequest { project_name, spec_path, constraints?, output_dir? } -> { job_id }
- GET  /api/ai-systems/build/status/{job_id} -> status, blueprint (when completed), current_phase, progress, error
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
POLL_INTERVAL = 5.0
MAX_POLL_WAIT = 3600.0


def _ai_systems_base_url() -> str:
    return (
        os.environ.get("PLANNING_AI_SYSTEMS_URL")
        or os.environ.get("UNIFIED_API_BASE_URL")
        or "http://localhost:8080"
    ).rstrip("/")


def start_ai_systems_build(
    project_name: str,
    spec_path: str,
    constraints: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
) -> Optional[str]:
    """
    Start an AI Systems build job. spec_path must be a path to a spec file on disk
    (AI Systems API expects a file path). Returns job_id or None on failure.
    """
    base = _ai_systems_base_url()
    url = f"{base}/api/ai-systems/build"
    payload: Dict[str, Any] = {
        "project_name": project_name,
        "spec_path": spec_path,
        "constraints": constraints or {},
    }
    if output_dir is not None:
        payload["output_dir"] = output_dir
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("job_id")
    except (httpx.HTTPError, httpx.TimeoutException, ValueError, KeyError) as e:
        logger.warning("AI Systems build start failed: %s", e)
        return None


def get_ai_systems_build_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get status of an AI Systems build job. Returns None on failure."""
    base = _ai_systems_base_url()
    url = f"{base}/api/ai-systems/build/status/{job_id}"
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        logger.warning("AI Systems build status failed for %s: %s", job_id, e)
        return None


def wait_for_ai_systems_build_completion(
    job_id: str,
    poll_interval: float = POLL_INTERVAL,
    max_wait: float = MAX_POLL_WAIT,
) -> Dict[str, Any]:
    """
    Poll until build is completed or failed. Returns dict with status and optional
    blueprint (when completed).
    """
    start = time.monotonic()
    while (time.monotonic() - start) < max_wait:
        status = get_ai_systems_build_status(job_id)
        if status is None:
            return {"status": "failed", "error": "Failed to get status"}
        s = status.get("status", "")
        if s == "completed":
            return status
        if s == "failed":
            return status
        time.sleep(poll_interval)
    return {"status": "failed", "error": "Timed out waiting for AI Systems build"}
