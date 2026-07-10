"""Temporal workflow + per-stage activities wrapping the market_research pipeline.

``MarketResearchWorkflow`` (in :mod:`workflows`) fans the pipeline out into one
Temporal activity per stage (defined in :mod:`activities`) — a single-shot
prepare/ingest/finalize plus one UX activity per transcript and the
psychology/consistency/viability/scripts specialist stages — so every agent
invocation is durable and individually retryable. Worker startup lives in
:mod:`worker` and is invoked by the team_service entrypoint at boot
(``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``), with the API
lifespan as a standalone-dev backstop, so the Temporal client is connected
before the API serves its first request. This package ``__init__`` must stay
free of import-time side effects (no worker boot, no ``os.getenv``) — the
temporalio sandbox replays it during workflow registration.
"""

from __future__ import annotations

from market_research_team.temporal.activities import (
    consistency_activity,
    finalize_activity,
    ingest_activity,
    mark_failed_activity,
    prepare_activity,
    psychology_activity,
    report_progress_activity,
    scripts_activity,
    ux_one_activity,
    viability_activity,
)
from market_research_team.temporal.workflows import MarketResearchWorkflow

WORKFLOWS = [MarketResearchWorkflow]
ACTIVITIES = [
    prepare_activity,
    ingest_activity,
    ux_one_activity,
    psychology_activity,
    consistency_activity,
    viability_activity,
    scripts_activity,
    report_progress_activity,
    mark_failed_activity,
    finalize_activity,
]
TASK_QUEUE = "market_research-queue"
WORKFLOW_ID_PREFIX = "market-research-"

__all__ = [
    "ACTIVITIES",
    "MarketResearchWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "consistency_activity",
    "finalize_activity",
    "ingest_activity",
    "mark_failed_activity",
    "prepare_activity",
    "psychology_activity",
    "report_progress_activity",
    "scripts_activity",
    "ux_one_activity",
    "viability_activity",
]
