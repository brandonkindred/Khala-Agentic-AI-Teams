"""Temporal activities for the social media marketing team."""

from __future__ import annotations

import logging
from typing import Any, Dict

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_social_marketing_team_job")
def run_team_job_activity(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Run the social marketing team job (or run or revise)."""
    try:
        from social_media_marketing_team.api.main import _run_team_job, RunMarketingTeamRequest
        request = RunMarketingTeamRequest(**request_dict)
        _run_team_job(job_id, request)
    except Exception as e:
        logger.exception("Social marketing team job activity failed for job %s", job_id)
        raise
