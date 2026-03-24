"""Temporal activities for the personal assistant team."""

from __future__ import annotations

import logging
from typing import Any, Dict

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="run_pa_assistant")
def run_assistant_activity(
    job_id: str,
    user_id: str,
    message: str,
    context: Dict[str, Any],
) -> None:
    """Run the assistant job (orchestrator handle_request with job updates)."""
    try:
        from personal_assistant_team.api.main import _run_assistant_job

        _run_assistant_job(job_id, user_id, message, context or {})
    except Exception:
        logger.exception("PA assistant activity failed for job %s", job_id)
        raise
