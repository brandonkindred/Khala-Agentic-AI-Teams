"""Temporal activities for the Planning V3 team."""

from __future__ import annotations

import logging
from typing import Optional

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_planning_v3_workflow")
def run_planning_v3_activity(
    job_id: str,
    repo_path: str,
    client_name: Optional[str],
    initial_brief: Optional[str],
    spec_content: Optional[str],
    use_product_analysis: bool,
    use_planning_v2: bool,
    use_market_research: bool,
) -> None:
    """Run the Planning V3 workflow (discovery and requirements)."""
    try:
        from planning_v3_team.api.main import _run_workflow_background
        _run_workflow_background(
            job_id,
            repo_path,
            client_name,
            initial_brief,
            spec_content,
            use_product_analysis,
            use_planning_v2,
            use_market_research,
        )
    except Exception as e:
        logger.exception("Planning V3 activity failed for job %s", job_id)
        raise
