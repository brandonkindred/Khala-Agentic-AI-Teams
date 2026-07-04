"""Temporal workflow + activity for the market_research team.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without
executing any non-deterministic top-level code (e.g. ``os.getenv``,
worker bootstrap). Co-locating ``start_team_worker``/``is_temporal_enabled``
with the workflow class trips the sandbox with
``__call__ on os.getenv restricted`` during workflow registration.

The activity reuses the thread path's shared pipeline core
(``_run_pipeline_core`` in ``market_research_team.api.main``) so the
job-store bookkeeping (RUNNING → COMPLETED, cancel guards) lives in exactly
one place. Status is written to the durable ``JobServiceClient`` store, so a
completed run survives a worker/process restart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn(name="market_research_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the market-research orchestrator and record job status.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store
          (the API endpoint calls ``create_job`` before dispatch).
        - ``request`` is the serialized ``RunMarketResearchRequest``
          (i.e. ``payload.model_dump()``).

    Postconditions:
        - On success the job store row ends in ``COMPLETED`` with the
          orchestrator result and the activity returns ``{"job_id": job_id}``.
        - If the job was cancelled, leaves the row untouched and returns
          ``{"job_id": job_id}`` (a cancelled run is terminal — not retried).
        - On a genuine failure, marks the row ``FAILED`` and re-raises so the
          failure surfaces as a failed Temporal workflow rather than a
          silently-"completed" one. Auto-retry is bounded by the workflow's
          ``RetryPolicy`` (see ``MarketResearchWorkflow.run``).
    """
    from market_research_team.api.main import (
        RunMarketResearchRequest,
        _build_mission,
        _run_pipeline_core,
    )
    from market_research_team.models import HumanReview
    from market_research_team.shared.job_store import (
        JOB_STATUS_FAILED,
        is_job_cancelled,
        update_job,
    )

    req = RunMarketResearchRequest(**request)
    mission = _build_mission(req)
    human_review = HumanReview(approved=req.human_approved, feedback=req.human_feedback)
    try:
        _run_pipeline_core(job_id, mission, human_review)
    except Exception as e:
        activity.logger.exception("Market research job %s failed", job_id)
        if is_job_cancelled(job_id):
            return {"job_id": job_id}
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
        raise
    return {"job_id": job_id}


@workflow.defn(name="MarketResearchWorkflow")
class MarketResearchWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the market-research pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``RunMarketResearchRequest``
              (``payload.model_dump()``).

        Postconditions:
            - Delegates to ``run_pipeline_activity`` (which owns job-store
              status bookkeeping) and returns its ``{"job_id": job_id}`` result.
        """
        return await workflow.execute_activity(
            run_pipeline_activity,
            args=[job_id, request],
            start_to_close_timeout=timedelta(hours=2),
            # The orchestrator is a long, non-idempotent LLM pipeline, and the
            # llm_service layer already fails over on transient provider errors
            # (429s, etc.). A workflow-level retry would therefore mostly re-run
            # expensive, deterministic failures. Cap at a single attempt: a
            # failure surfaces as a failed workflow + a FAILED job-store row for
            # explicit resubmission rather than being auto-retried. (Worker or
            # process crashes are still durably re-dispatched by Temporal — that
            # is unaffected by this attempt cap.)
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
