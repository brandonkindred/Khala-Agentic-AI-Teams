"""Temporal activities for the blogging team. Each activity runs the pipeline with job store updates."""

from __future__ import annotations

import logging
from typing import Any, Dict

from temporalio import activity

from blogging.shared.run_pipeline_job import run_blog_full_pipeline_job

logger = logging.getLogger(__name__)


@activity.defn(name="run_blog_full_pipeline")
def run_full_pipeline_activity(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Execute the full blog pipeline (research -> planning -> draft -> copy-edit) and update job store."""
    try:
        run_blog_full_pipeline_job(job_id, request_dict)
    except Exception:
        logger.exception("Blog full pipeline activity failed for job %s", job_id)
        raise
