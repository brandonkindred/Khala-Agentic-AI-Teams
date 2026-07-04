"""Temporal workflow + activity wrapping the market research orchestrator.

The workflow class and activity live in :mod:`workflows` (sandbox-safe —
no top-level non-deterministic calls). Worker startup lives in
:mod:`worker` and is invoked by the team_service entrypoint at boot
(``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``), with the
API lifespan as a standalone-dev backstop, so the Temporal client is
connected before the API serves its first request. This package
``__init__`` must stay free of import-time side effects (no worker boot,
no ``os.getenv``) — the temporalio sandbox replays it during workflow
registration.
"""

from __future__ import annotations

from market_research_team.temporal.workflows import (
    MarketResearchWorkflow,
    run_pipeline_activity,
)

WORKFLOWS = [MarketResearchWorkflow]
ACTIVITIES = [run_pipeline_activity]
TASK_QUEUE = "market_research-queue"
WORKFLOW_ID_PREFIX = "market-research-"

__all__ = [
    "ACTIVITIES",
    "MarketResearchWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "run_pipeline_activity",
]
