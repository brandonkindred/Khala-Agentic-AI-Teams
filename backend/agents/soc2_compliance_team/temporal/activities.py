"""Temporal activities for the SOC2 compliance team."""

from __future__ import annotations

import logging

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_soc2_audit")
def run_audit_activity(job_id: str, repo_path: str) -> None:
    """Run the SOC2 audit job."""
    try:
        from soc2_compliance_team.api.main import _run_audit_job

        _run_audit_job(job_id, repo_path)
    except Exception:
        logger.exception("SOC2 audit activity failed for job %s", job_id)
        raise
