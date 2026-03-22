"""Temporal activities for the Agent Provisioning team."""

from __future__ import annotations

import logging

from temporalio import activity

from agent_provisioning_team.models import AccessTier

logger = logging.getLogger(__name__)


@activity.defn(name="run_agent_provisioning")
def run_provisioning_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier_str: str,
) -> None:
    """Run the provisioning workflow. Converts access_tier_str to AccessTier."""
    try:
        from agent_provisioning_team.api.main import _run_provisioning_background
        access_tier = AccessTier(access_tier_str)
        _run_provisioning_background(job_id, agent_id, manifest_path, access_tier)
    except Exception:
        logger.exception("Agent Provisioning activity failed for job %s", job_id)
        raise
