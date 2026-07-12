"""Temporal workflows and activities for the social media marketing team.

Pattern A: this package exports ``WORKFLOWS`` and ``ACTIVITIES`` so
``shared_temporal.start_team_worker`` can register a worker for the team. The
workflow class lives in :mod:`workflows` (sandbox-safe -- no top-level
non-deterministic calls); worker startup lives in :mod:`worker`. This ``__init__``
must stay free of import-time side effects (no worker boot, no ``os.getenv``) -- the
temporalio sandbox replays it during workflow registration.
"""

from __future__ import annotations

from social_media_marketing_team.temporal.activities import (
    consensus_stage_activity,
    content_plan_stage_activity,
    experiment_stage_activity,
    finalize_stage_activity,
    platform_stage_activity,
    run_team_job_activity,
)
from social_media_marketing_team.temporal.client import is_temporal_enabled
from social_media_marketing_team.temporal.constants import TASK_QUEUE
from social_media_marketing_team.temporal.workflows import SocialMarketingTeamWorkflow

WORKFLOWS = [SocialMarketingTeamWorkflow]
ACTIVITIES = [
    consensus_stage_activity,
    content_plan_stage_activity,
    platform_stage_activity,
    experiment_stage_activity,
    finalize_stage_activity,
    run_team_job_activity,
]

__all__ = [
    "ACTIVITIES",
    "SocialMarketingTeamWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "consensus_stage_activity",
    "content_plan_stage_activity",
    "experiment_stage_activity",
    "finalize_stage_activity",
    "is_temporal_enabled",
    "platform_stage_activity",
    "run_team_job_activity",
]
