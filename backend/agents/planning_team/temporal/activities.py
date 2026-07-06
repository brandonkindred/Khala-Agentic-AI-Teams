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
    """Temporal activity that runs the full Planning workflow for one job.

    Delegates to `run_workflow_background` (the same logic the thread-mode
    `/run` endpoint uses) so both dispatch paths share one implementation.
    Runs intake through document_production/sub_agent_provisioning and
    updates the job store as it progresses; the caller polls job status
    rather than awaiting a return value here.

    Args:
        job_id: The job identifier to update as the workflow progresses.
        repo_path: Resolved workspace path for artifacts.
        client_name: Optional client/organization name for context.
        initial_brief: Optional initial brief; required unless spec_content is set.
        spec_content: Optional starting spec; required unless initial_brief is set.
        use_product_analysis: Whether to call Product Requirements Analysis.
        use_market_research: Whether to call Market Research for discovery.

    Raises:
        Exception: Re-raised after being logged, so Temporal's retry policy
            (see `WORKFLOW_TIMEOUT`/`DEFAULT_RETRY_POLICY` in workflows.py)
            observes the failure; `run_workflow_background` itself marks the
            job failed on its own errors before this activity sees them.
    """
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
