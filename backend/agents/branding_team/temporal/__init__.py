"""Temporal workflow + activity package for the Branding team.

Follows shared.temporal Pattern A: this package exports ``WORKFLOWS`` /
``ACTIVITIES`` and self-boots a worker on import when Temporal is enabled. The
generic ``team_service`` entrypoint also boots the worker via
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``
(``branding_team.temporal.worker:start_branding_temporal_worker_thread``);
both are idempotent because ``start_team_worker`` reuses a live thread per team.

The pipeline is decomposed into per-phase activities orchestrated by
``BrandingWorkflow`` (see ``workflows.py`` / ``activities.py``): begin → phase
1..N → optional integrations → finalize, with per-phase checkpoints, a
``progress`` query, and a ``cancel`` signal.
"""

from __future__ import annotations

from branding_team.temporal.activities import (
    begin_branding_job_activity,
    check_branding_cancelled_activity,
    finalize_branding_activity,
    mark_branding_failed_activity,
    run_branding_phase_activity,
    run_design_assets_activity,
    run_market_research_activity,
)
from branding_team.temporal.constants import TASK_QUEUE
from branding_team.temporal.workflows import BrandingWorkflow

WORKFLOWS = [BrandingWorkflow]
ACTIVITIES = [
    begin_branding_job_activity,
    run_branding_phase_activity,
    run_market_research_activity,
    run_design_assets_activity,
    finalize_branding_activity,
    mark_branding_failed_activity,
    check_branding_cancelled_activity,
]

__all__ = [
    "ACTIVITIES",
    "WORKFLOWS",
    "BrandingWorkflow",
    "begin_branding_job_activity",
    "check_branding_cancelled_activity",
    "finalize_branding_activity",
    "mark_branding_failed_activity",
    "run_branding_phase_activity",
    "run_design_assets_activity",
    "run_market_research_activity",
]

from shared.temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("branding", WORKFLOWS, ACTIVITIES, task_queue=TASK_QUEUE)
