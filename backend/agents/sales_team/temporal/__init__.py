"""Temporal workflows + per-agent activities wrapping the sales pod orchestrator.

``SalesWorkflow`` (in :mod:`workflows`) fans the main pipeline out into one
Temporal activity per prospect per stage (defined in :mod:`activities`), and
``DeepResearchWorkflow`` (in :mod:`deep_research_workflow`) fans the deep-research
prospecting pipeline out per-company and per-prospect (activities in
:mod:`deep_research_activities`) — so every specialist agent invocation is
durable and individually retryable. Worker startup lives in :mod:`worker` and is
invoked by the team_service entrypoint at boot (``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC``), with the API lifespan as a standalone-dev
backstop, so the Temporal client is connected before the API serves its first
request. This package ``__init__`` must stay free of import-time side effects (no
worker boot, no ``os.getenv``) — the temporalio sandbox replays it during
workflow registration.
"""

from __future__ import annotations

from sales_team.temporal.activities import (
    close_one_activity,
    coach_activity,
    discovery_one_activity,
    finalize_sales_pipeline_activity,
    load_dossiers_activity,
    mark_failed_activity,
    nurture_one_activity,
    outreach_one_activity,
    prepare_sales_pipeline_activity,
    proposal_one_activity,
    prospect_activity,
    qualify_one_activity,
    report_progress_activity,
)
from sales_team.temporal.deep_research_activities import (
    build_dossier_one_activity,
    companies_activity,
    finalize_deep_research_activity,
    map_company_one_activity,
    prepare_deep_research_activity,
    rank_activity,
)
from sales_team.temporal.deep_research_workflow import DeepResearchWorkflow
from sales_team.temporal.workflows import SalesWorkflow

WORKFLOWS = [SalesWorkflow, DeepResearchWorkflow]
ACTIVITIES = [
    # main pipeline
    prepare_sales_pipeline_activity,
    prospect_activity,
    load_dossiers_activity,
    outreach_one_activity,
    qualify_one_activity,
    nurture_one_activity,
    discovery_one_activity,
    proposal_one_activity,
    close_one_activity,
    coach_activity,
    report_progress_activity,
    mark_failed_activity,
    finalize_sales_pipeline_activity,
    # deep research (reuses report_progress + mark_failed above)
    prepare_deep_research_activity,
    companies_activity,
    map_company_one_activity,
    rank_activity,
    build_dossier_one_activity,
    finalize_deep_research_activity,
]
TASK_QUEUE = "sales-queue"
WORKFLOW_ID_PREFIX = "sales-"
DEEP_RESEARCH_WORKFLOW_ID_PREFIX = "sales-deep-research-"

__all__ = [
    "ACTIVITIES",
    "DEEP_RESEARCH_WORKFLOW_ID_PREFIX",
    "DeepResearchWorkflow",
    "SalesWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "build_dossier_one_activity",
    "close_one_activity",
    "coach_activity",
    "companies_activity",
    "discovery_one_activity",
    "finalize_deep_research_activity",
    "finalize_sales_pipeline_activity",
    "load_dossiers_activity",
    "map_company_one_activity",
    "mark_failed_activity",
    "nurture_one_activity",
    "outreach_one_activity",
    "prepare_deep_research_activity",
    "prepare_sales_pipeline_activity",
    "proposal_one_activity",
    "prospect_activity",
    "qualify_one_activity",
    "rank_activity",
    "report_progress_activity",
]
