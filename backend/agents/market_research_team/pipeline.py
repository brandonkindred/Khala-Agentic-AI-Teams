"""Pipeline execution for the market-research team.

Neutral module (no FastAPI, no Temporal) holding the mission construction and
the cancel-guarded job-store bookkeeping shared by the HTTP thread-dispatch
path (``api.main``) and the Temporal activity (``temporal.workflows``). Keeping
it here lets the durable worker run the pipeline without importing the web app.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

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

# The retired Strands graph enforced a 600s whole-pipeline ceiling
# (``build_research_graph``'s ``set_execution_timeout``). Nothing bounds the
# per-stage-seam thread path (or the legacy Temporal drain-out activity, which
# shares this same core), so a stuck LLM provider could otherwise hang a run
# indefinitely. Restores an equivalent ceiling, env-configurable.
_DEFAULT_PIPELINE_TIMEOUT_S = 600.0
_MAX_PIPELINE_TIMEOUT_S = 3600.0


def _pipeline_timeout_s() -> float:
    """Overall pipeline deadline (seconds) for the thread/legacy-activity path.

    Preconditions:
        - None (environment may be unset or hold garbage).
    Postconditions:
        - Returns ``MARKET_RESEARCH_PIPELINE_TIMEOUT_S`` clamped to
          ``[30, _MAX_PIPELINE_TIMEOUT_S]`` (garbage/unset → the 600s default).
    """
    from shared.env_config import env_float

    return env_float(
        "MARKET_RESEARCH_PIPELINE_TIMEOUT_S",
        _DEFAULT_PIPELINE_TIMEOUT_S,
        floor=30.0,
        ceiling=_MAX_PIPELINE_TIMEOUT_S,
    )


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


def prepare(req: RunMarketResearchRequest) -> tuple[ResearchMission, HumanReview]:
    """Build the ``(mission, human_review)`` orchestrator inputs from a request.

    Single seam used by both dispatch paths (the HTTP thread branch and the
    Temporal activity, which reconstructs ``req`` from a dict) so the two never
    derive their inputs independently.

    Preconditions:
        - ``req`` is a validated ``RunMarketResearchRequest``.

    Postconditions:
        - Returns ``(ResearchMission, HumanReview)`` derived entirely from
          ``req``.
    """
    return (
        build_mission(req),
        HumanReview(approved=req.human_approved, feedback=req.human_feedback),
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
        - Raises ``TimeoutError`` if the orchestrator exceeds
          ``_pipeline_timeout_s()`` (restores the ceiling the retired Strands
          graph enforced). Note this bounds how long the CALLER waits, not the
          orchestrator thread itself — Python cannot forcibly interrupt a
          blocking call, so a timed-out run's thread keeps executing in the
          background until its current LLM call returns, then exits with its
          result discarded; the job is reported FAILED to the caller well
          before that.
        - Otherwise propagates any orchestrator exception unchanged — the
          caller owns the failure policy (swallow vs. re-raise).
    """
    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_RUNNING)
    timeout_s = _pipeline_timeout_s()
    # Not a `with ThreadPoolExecutor(...) as pool:` block: the context manager's
    # __exit__ calls shutdown(wait=True), which would block this thread until the
    # orchestrator finishes regardless of the timeout below, defeating the point
    # of bounding the caller's wait. shutdown(wait=False) lets the orchestrator
    # thread keep running in the background (per the docstring above) while this
    # call returns as soon as the timeout fires.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-research-pipeline")
    future = pool.submit(MarketResearchOrchestrator().run, mission, human_review)
    try:
        result = future.result(timeout=timeout_s)
    except FutureTimeoutError as exc:
        pool.shutdown(wait=False)
        raise TimeoutError(
            f"Market research pipeline for job {job_id} exceeded {timeout_s:.0f}s"
        ) from exc
    pool.shutdown(wait=False)
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
