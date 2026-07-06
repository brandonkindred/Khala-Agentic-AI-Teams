"""Temporal activities for the Planning team."""

from __future__ import annotations

import logging
from typing import Optional

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_planning_activity")
def run_planning_activity(
    job_id: str,
    repo_path: str,
    client_name: Optional[str],
    initial_brief: Optional[str],
    spec_content: Optional[str],
    use_product_analysis: bool,
    use_market_research: bool,
) -> None:
    """Run the Planning workflow (discovery and requirements)."""
    try:
        from planning_team.api.main import run_workflow_background

        run_workflow_background(
            job_id,
            repo_path,
            client_name,
            initial_brief,
            spec_content,
            use_product_analysis,
            use_market_research,
        )
    except Exception:
        logger.exception("Planning activity failed for job %s", job_id)
        raise
