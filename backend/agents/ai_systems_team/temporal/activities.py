"""Temporal activities for the AI systems team."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_ai_systems_build")
def run_build_activity(
    job_id: str,
    project_name: str,
    spec_path: str,
    constraints: Dict[str, Any],
    output_dir: Optional[str],
) -> None:
    """Run the AI system build (orchestrator) with job updates."""
    try:
        from ai_systems_team.api.main import _run_build_background
        _run_build_background(job_id, project_name, spec_path, constraints, output_dir)
    except Exception:
        logger.exception("AI Systems build activity failed for job %s", job_id)
        raise
