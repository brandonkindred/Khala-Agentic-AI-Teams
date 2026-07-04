"""Temporal workflow + activity for the market_research team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without
executing any non-deterministic top-level code (e.g. ``os.getenv``,
worker bootstrap). Co-locating ``start_team_worker``/``is_temporal_enabled``
with the workflow class trips the sandbox with
``__call__ on os.getenv restricted`` during workflow registration.

The activity owns the same job-store bookkeeping the thread path performs
in ``market_research_team.api.main._run_market_research_background`` — it
must, or a Temporal-dispatched job would never leave ``pending`` from the
``/market-research/status`` endpoint's perspective. Status is written to the
durable ``JobServiceClient`` store, so a completed run survives a
worker/process restart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="market_research_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the market-research orchestrator and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store
          (the API endpoint calls ``create_job`` before dispatch).
        - ``request`` is the serialized ``RunMarketResearchRequest``
          (i.e. ``payload.model_dump()``).

    Postconditions:
        - The job store row for ``job_id`` ends in ``COMPLETED`` (with the
          orchestrator result) on success, ``FAILED`` (with the error) on
          exception, and is left untouched when the job was cancelled.
        - Returns ``{"job_id": job_id}``.
    """
    from market_research_team.api.main import RunMarketResearchRequest
    from market_research_team.models import HumanReview, ResearchMission
    from market_research_team.orchestrator import MarketResearchOrchestrator
    from market_research_team.shared.job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_RUNNING,
        is_job_cancelled,
        update_job,
    )

    req = RunMarketResearchRequest(**request)
    try:
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        update_job(job_id, status=JOB_STATUS_RUNNING)
        mission = ResearchMission(
            product_concept=req.product_concept,
            target_users=req.target_users,
            business_goal=req.business_goal,
            topology=req.topology,
            transcript_folder_path=req.transcript_folder_path,
            transcripts=req.transcripts,
        )
        human_review = HumanReview(approved=req.human_approved, feedback=req.human_feedback)
        result = MarketResearchOrchestrator().run(mission, human_review)
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except Exception as e:
        activity.logger.exception("Market research job %s failed", job_id)
        if not is_job_cancelled(job_id):
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
    return {"job_id": job_id}


@workflow.defn(name="MarketResearchWorkflow")
class MarketResearchWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            args=[job_id, request],
            start_to_close_timeout=timedelta(hours=2),
        )
