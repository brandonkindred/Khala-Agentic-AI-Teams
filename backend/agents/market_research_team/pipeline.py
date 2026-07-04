"""Pipeline execution for the market-research team.

Neutral module (no FastAPI, no Temporal) holding the mission construction and
the cancel-guarded job-store bookkeeping shared by the HTTP thread-dispatch
path (``api.main``) and the Temporal activity (``temporal.workflows``). Keeping
it here lets the durable worker run the pipeline without importing the web app.
"""

from __future__ import annotations

import logging

from market_research_team.models import HumanReview, ResearchMission, RunMarketResearchRequest
from market_research_team.orchestrator import MarketResearchOrchestrator
from market_research_team.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    is_job_cancelled,
    update_job,
)

logger = logging.getLogger(__name__)


def build_mission(req: RunMarketResearchRequest) -> ResearchMission:
    """Map a ``RunMarketResearchRequest`` onto a ``ResearchMission``.

    Preconditions:
        - ``req`` is a validated ``RunMarketResearchRequest``.

    Postconditions:
        - Returns a ``ResearchMission`` carrying every mission field from
          ``req`` (single source of truth for both the thread dispatch path
          and the Temporal activity, which reconstructs ``req`` from a dict).
    """
    return ResearchMission(
        product_concept=req.product_concept,
        target_users=req.target_users,
        business_goal=req.business_goal,
        topology=req.topology,
        transcript_folder_path=req.transcript_folder_path,
        transcripts=req.transcripts,
    )


def run_pipeline_core(job_id: str, mission: ResearchMission, human_review: HumanReview) -> None:
    """Run the orchestrator with cancel guards + RUNNING/COMPLETED bookkeeping.

    Shared by the thread dispatch path and the Temporal activity so the
    status-write order and cancel semantics live in one place.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - Writes RUNNING then COMPLETED (with the orchestrator result) on
          success; writes nothing and returns early if the job is cancelled
          before or after the run.
        - Propagates any orchestrator exception unchanged — the caller owns
          the failure policy (swallow vs. re-raise).
    """
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_RUNNING)
    result = MarketResearchOrchestrator().run(mission, human_review)
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())


def run_market_research_background(
    job_id: str, mission: ResearchMission, human_review: HumanReview
) -> None:
    """Thread-path runner: execute the pipeline and swallow failures as FAILED.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.

    Postconditions:
        - On orchestrator failure, marks the job FAILED (unless it was
          cancelled) and returns — a daemon thread has no caller to raise to.
    """
    try:
        run_pipeline_core(job_id, mission, human_review)
    except Exception as e:
        logger.exception("Market research job %s failed", job_id)
        if not is_job_cancelled(job_id):
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
