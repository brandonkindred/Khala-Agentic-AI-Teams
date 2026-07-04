"""Temporal workflow + activity package for the Branding team.

Follows shared_temporal Pattern A: this package exports ``WORKFLOWS`` /
``ACTIVITIES`` and self-boots a worker on import when Temporal is enabled. The
generic ``team_service`` entrypoint also boots the worker via
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``
(``branding_team.temporal.worker:start_branding_temporal_worker_thread``);
both are idempotent because ``start_team_worker`` reuses a live thread per team.
"""

from __future__ import annotations

from branding_team.temporal.activities import run_branding_pipeline_activity
from branding_team.temporal.constants import TASK_QUEUE
from branding_team.temporal.workflows import BrandingWorkflow

WORKFLOWS = [BrandingWorkflow]
ACTIVITIES = [run_branding_pipeline_activity]

__all__ = [
    "ACTIVITIES",
    "WORKFLOWS",
    "BrandingWorkflow",
    "run_branding_pipeline_activity",
]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("branding", WORKFLOWS, ACTIVITIES, task_queue=TASK_QUEUE)
