"""Temporal activities for the Branding team.

A single activity wraps the branding pipeline. It reconstructs the request
models from a JSON-safe ``payload`` dict (the only thing Temporal can carry
across the workflow/activity boundary) and delegates to the existing
``_run_branding_background`` job function, which already owns the
RUNNING -> COMPLETED/FAILED job-store bookkeeping. This mirrors the v1
``run_provisioning_activity`` -> ``_run_provisioning_background`` pattern in
``agent_provisioning_team`` and keeps the thread path and Temporal path running
the exact same pipeline body.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="branding_run_pipeline")
def run_branding_pipeline_activity(payload: dict[str, Any]) -> None:
    """Run one branding job from a serialized payload.

    Preconditions:
        - ``payload`` contains a ``job_id`` (str) whose job row already exists,
          plus a ``mission`` dict and a ``human_review`` dict that validate
          against ``BrandingMission`` / ``HumanReview``.
        - ``payload['target_phase']`` is ``None`` or a valid ``BrandPhase``
          value string.
    Postconditions:
        - Delegates to ``_run_branding_background``, which transitions the job
          row to COMPLETED (with the serialized ``TeamOutput``), FAILED, or
          leaves it untouched when the run was cancelled.
        - On a FAILED terminal state the activity re-raises so the failure
          surfaces as a failed Temporal workflow rather than a silently
          "completed" one (the job row remains the source of truth for the API
          poller); returns ``None`` on COMPLETED or cancelled.
    """
    from branding_team.api.main import _run_branding_background
    from branding_team.models import (
        BrandCheckRequest,
        BrandingMission,
        BrandPhase,
        HumanReview,
    )
    from branding_team.shared.job_store import JOB_STATUS_FAILED, get_job

    mission = BrandingMission(**payload["mission"])
    human_review = HumanReview(**payload["human_review"])
    brand_checks = [BrandCheckRequest(**c) for c in payload.get("brand_checks") or []]
    tp = payload.get("target_phase")
    target_phase = BrandPhase(tp) if tp else None

    job_id = payload["job_id"]
    _run_branding_background(
        job_id,
        mission,
        human_review,
        brand_checks,
        payload.get("client_id"),
        payload.get("brand_id"),
        bool(payload.get("include_market_research")),
        bool(payload.get("include_design_assets")),
        target_phase,
    )

    # _run_branding_background swallows pipeline errors into a FAILED job row.
    # Re-raise on that terminal state so the Temporal workflow reflects the
    # failure instead of reporting a green "completed" run.
    job = get_job(job_id)
    if job is not None and job.get("status") == JOB_STATUS_FAILED:
        raise RuntimeError(f"Branding job {job_id} failed: {job.get('error')}")
