"""FastAPI endpoints for the AI Sales Team pod."""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
    start_stale_job_monitor,
)
from sales_team.learning_engine import LearningEngine
from sales_team.models import (
    CoachingRequest,
    DealOutcome,
    DeepResearchRequest,
    DeepResearchResult,
    LearningInsights,
    NurtureRequest,
    OutreachRequest,
    ProposalRequest,
    ProspectDossier,
    ProspectingRequest,
    QualificationRequest,
    RecordDealOutcomeRequest,
    RecordStageOutcomeRequest,
    SalesPipelineRequest,
    SalesPipelineResult,
    StageOutcome,
)
from sales_team.orchestrator import SalesPodOrchestrator
from sales_team.outcome_store import (
    load_current_insights,
    load_deal_outcomes,
    load_stage_outcomes,
    outcome_counts,
    record_deal_outcome,
    record_stage_outcome,
)
from shared_observability import init_otel, instrument_fastapi_app

init_otel(service_name="sales-team", team_key="sales_team")


@asynccontextmanager
async def _sales_lifespan(_app: FastAPI):
    """Register the sales_team Postgres schema at startup and close the pool at shutdown.

    Both steps are best-effort — when ``POSTGRES_HOST`` is unset (e.g. in
    isolated unit tests) ``shared_postgres`` is a no-op and we continue.
    """
    try:
        from sales_team.postgres import SCHEMA as SALES_POSTGRES_SCHEMA
        from shared_postgres import register_team_schemas

        register_team_schemas(SALES_POSTGRES_SCHEMA)
    except Exception:
        logger.exception("sales_team postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("sales_team shared_postgres close_pool failed", exc_info=True)


app = FastAPI(
    title="AI Sales Team API",
    version="1.0.0",
    description=(
        "Full-stack B2B sales pod powered by AWS Strands agents. "
        "Handles prospecting, cold outreach, lead qualification, nurturing, "
        "discovery, proposals, and closing — grounded in Gong Labs, Jeb Blount, "
        "HubSpot, Anthony Iannarino, Jill Konrath, Sales Hacker, Salesfolk, and Zig Ziglar."
    ),
    lifespan=_sales_lifespan,
)
instrument_fastapi_app(app, team_key="sales_team")

logger = logging.getLogger(__name__)
_job_manager = JobServiceClient(team="sales_team")
_stale_monitor_stop = start_stale_job_monitor(
    _job_manager,
    interval_seconds=15.0,
    stale_after_seconds=300.0,
    reason="Job heartbeat stale while pending/running",
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SalesPipelineRunResponse(BaseModel):
    job_id: str
    status: str
    message: str


class SalesPipelineStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    product_name: str
    last_updated_at: str
    eta_hint: Optional[str] = None
    error: Optional[str] = None
    result: Optional[SalesPipelineResult] = None


class SalesPipelineJobListItem(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    product_name: str
    created_at: Optional[str] = None
    last_updated_at: Optional[str] = None


class CancelJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _update_job(job_id: str, **fields: Any) -> None:
    _job_manager.update_job(job_id, **fields)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all active sales pipeline jobs as failed (called on server shutdown)."""
    _job_manager.mark_stale_active_jobs_failed(stale_after_seconds=0, reason=reason)


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------


def _run_pipeline_job(job_id: str, request: SalesPipelineRequest) -> None:
    try:
        _update_job(
            job_id,
            status=JOB_STATUS_RUNNING,
            current_stage="initializing",
            progress=2,
            eta_hint="Starting pipeline...",
        )

        orchestrator = SalesPodOrchestrator()

        def on_update(stage: str, pct: int) -> None:
            _update_job(job_id, current_stage=stage, progress=pct, last_updated_at=_now())

        result = orchestrator.run(request, job_id=job_id, update_cb=on_update)

        _update_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            current_stage="completed",
            progress=100,
            eta_hint="done",
            result=result.model_dump(),
            last_updated_at=_now(),
        )
    except Exception as exc:
        logger.error("Sales pipeline job %s failed: %s", job_id, exc, exc_info=True)
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            current_stage="failed",
            error=str(exc),
            eta_hint=None,
            last_updated_at=_now(),
        )


# ---------------------------------------------------------------------------
# Pipeline endpoints (async, job-based)
# ---------------------------------------------------------------------------


@app.post("/sales/pipeline/run", response_model=SalesPipelineRunResponse, tags=["pipeline"])
def run_pipeline(request: SalesPipelineRequest) -> SalesPipelineRunResponse:
    """Start a full sales pipeline run from the specified entry stage.

    Returns a job_id to poll for status and results.
    Entry stages: prospecting → outreach → qualification → nurturing → discovery → proposal → negotiation.
    """
    job_id = str(uuid.uuid4())
    now = _now()
    _job_manager.create_job(
        job_id,
        job_type="sales_pipeline",
        status=JOB_STATUS_PENDING,
        current_stage="queued",
        progress=0,
        product_name=request.product_name,
        entry_stage=request.entry_stage.value,
        result=None,
        error=None,
        eta_hint="queued",
        created_at=now,
        last_updated_at=now,
    )
    thread = threading.Thread(target=_run_pipeline_job, args=(job_id, request), daemon=True)
    thread.start()
    return SalesPipelineRunResponse(
        job_id=job_id,
        status=JOB_STATUS_RUNNING,
        message=(
            f"Sales pipeline started (entry: {request.entry_stage.value}). "
            f"Poll GET /sales/pipeline/status/{job_id} for updates."
        ),
    )


@app.get(
    "/sales/pipeline/status/{job_id}",
    response_model=SalesPipelineStatusResponse,
    tags=["pipeline"],
)
def get_pipeline_status(job_id: str) -> SalesPipelineStatusResponse:
    """Poll the status of a running or completed pipeline job."""
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result_data = job.get("result")
    result_obj = None
    if result_data and isinstance(result_data, dict):
        try:
            result_obj = SalesPipelineResult(**result_data)
        except Exception:
            result_obj = None

    return SalesPipelineStatusResponse(
        job_id=job_id,
        status=job.get("status", JOB_STATUS_PENDING),
        current_stage=job.get("current_stage", ""),
        progress=job.get("progress", 0),
        product_name=job.get("product_name", ""),
        last_updated_at=job.get("last_updated_at", _now()),
        eta_hint=job.get("eta_hint"),
        error=job.get("error"),
        result=result_obj,
    )


@app.get(
    "/sales/pipeline/jobs",
    response_model=List[SalesPipelineJobListItem],
    tags=["pipeline"],
)
def list_pipeline_jobs(running_only: bool = False) -> List[SalesPipelineJobListItem]:
    """List all sales pipeline jobs, optionally filtered to active jobs."""
    jobs = _job_manager.list_jobs()
    if running_only:
        jobs = [j for j in jobs if j.get("status") in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING)]
    items = [
        SalesPipelineJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            current_stage=j.get("current_stage", ""),
            progress=j.get("progress", 0),
            product_name=j.get("product_name", ""),
            created_at=j.get("created_at"),
            last_updated_at=j.get("last_updated_at"),
        )
        for j in jobs
    ]
    items.sort(key=lambda x: x.created_at or x.last_updated_at or "", reverse=True)
    return items


@app.post(
    "/sales/pipeline/job/{job_id}/cancel",
    response_model=CancelJobResponse,
    tags=["pipeline"],
)
def cancel_pipeline_job(job_id: str) -> CancelJobResponse:
    """Cancel a pending or running pipeline job."""
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current = job.get("status", JOB_STATUS_PENDING)
    if current not in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING):
        raise HTTPException(status_code=400, detail=f"Job is already in terminal state: {current}")
    _job_manager.update_job(job_id, status="cancelled", heartbeat=False)
    return CancelJobResponse(job_id=job_id)


@app.delete(
    "/sales/pipeline/job/{job_id}",
    response_model=DeleteJobResponse,
    tags=["pipeline"],
)
def delete_pipeline_job(job_id: str) -> DeleteJobResponse:
    """Delete a pipeline job from the store.

    Returns 404 if the job does not exist and 409 if the job is still
    pending or running (cancel it first before deleting).
    """
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current_status = job.get("status", JOB_STATUS_PENDING)
    if current_status in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete an active job (status={current_status}). Cancel it first.",
        )
    if not _job_manager.delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteJobResponse(job_id=job_id, message="Job deleted.")


# ---------------------------------------------------------------------------
# Synchronous single-stage endpoints
# ---------------------------------------------------------------------------


@app.post("/sales/prospect", tags=["stages"])
def prospect(request: ProspectingRequest) -> Dict[str, Any]:
    """Identify prospects matching the ICP using the Prospector agent.

    Grounded in Jeb Blount's Fanatical Prospecting and Sales Hacker ICP frameworks.
    """
    orchestrator = SalesPodOrchestrator()
    prospects = orchestrator.prospect_only(
        icp=request.icp,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        max_prospects=request.max_prospects,
        company_context=request.company_context,
    )
    return {"prospects": [p.model_dump() for p in prospects], "count": len(prospects)}


@app.post("/sales/outreach", tags=["stages"])
def generate_outreach(request: OutreachRequest) -> Dict[str, Any]:
    """Generate personalized cold outreach sequences for a list of prospects.

    Grounded in Salesfolk, Jill Konrath SNAP, and the Jeb Blount 6-touch cadence.
    Every prospect must have a dossier in the store (keyed by ``prospect.id``);
    prospects without one are skipped with a ``sales.outreach.dossier_missing``
    log line and do not appear in ``sequences`` or ``skipped_prospects``
    beyond a count.
    """
    orchestrator = SalesPodOrchestrator()
    dossier_map = orchestrator.load_dossiers_for_prospects(request.prospects)
    sequences = orchestrator.outreach_only(
        prospects=request.prospects,
        dossier_map=dossier_map,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        case_study_snippets=request.case_study_snippets,
        company_context=request.company_context,
    )
    skipped = [p.id for p in request.prospects if p.id not in dossier_map]
    return {
        "sequences": [s.model_dump() for s in sequences],
        "count": len(sequences),
        "skipped_prospect_ids": skipped,
    }


@app.post("/sales/qualify", tags=["stages"])
def qualify_lead(request: QualificationRequest) -> Dict[str, Any]:
    """Qualify a prospect using BANT, MEDDIC, and Iannarino's value-creation tiers.

    Returns a score, recommended action (advance / nurture / disqualify), and coaching notes.
    """
    orchestrator = SalesPodOrchestrator()
    score = orchestrator.qualify_only(
        prospect=request.prospect,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        call_notes=request.call_notes,
    )
    if not score:
        raise HTTPException(status_code=500, detail="Qualification agent failed to return a result")
    return score.model_dump()


@app.post("/sales/nurture", tags=["stages"])
def build_nurture(request: NurtureRequest) -> Dict[str, Any]:
    """Build long-cycle nurture sequences for leads not ready to buy.

    Grounded in HubSpot inbound methodology and Gong Labs cadence research.
    """
    orchestrator = SalesPodOrchestrator()
    sequences = orchestrator.nurture_only(
        prospects=request.prospects,
        product_name=request.product_name,
        value_proposition=request.value_proposition,
        duration_days=request.duration_days,
    )
    return {"sequences": [s.model_dump() for s in sequences], "count": len(sequences)}


@app.post("/sales/proposal", tags=["stages"])
def write_proposal(request: ProposalRequest) -> Dict[str, Any]:
    """Generate a structured sales proposal with ROI model.

    Grounded in Anthony Iannarino's Level-4 Value Creation proposal methodology.
    """
    orchestrator = SalesPodOrchestrator()
    proposal = orchestrator.propose_only(request)
    if not proposal:
        raise HTTPException(status_code=500, detail="Proposal agent failed to return a result")
    return proposal.model_dump()


@app.post("/sales/coaching", tags=["stages"])
def get_coaching(request: CoachingRequest) -> Dict[str, Any]:
    """Generate a Gong Labs-style pipeline coaching report.

    Flags deal risk signals, gives talk/listen ratio advice, and recommends next actions.
    """
    orchestrator = SalesPodOrchestrator()
    report = orchestrator.coach_only(
        prospects=request.prospects,
        product_name=request.product_name,
        pipeline_context=request.pipeline_context,
    )
    if not report:
        raise HTTPException(status_code=500, detail="Coach agent failed to return a result")
    return report.model_dump()


# ---------------------------------------------------------------------------
# Deep-research prospecting — top-N prospects + per-prospect dossiers
# ---------------------------------------------------------------------------


def _load_dossier_store():
    """Import and instantiate DossierStore, mapping failures to HTTP 503.

    Covers both import-time errors (e.g. psycopg missing in a stripped
    environment) and construction-time errors. Runtime call failures are
    handled by the individual route handlers so that 404 vs. 503 semantics
    can be preserved.
    """
    try:
        from sales_team.dossier_store import DossierStore
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Dossier store unavailable: {exc}") from exc
    try:
        return DossierStore()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Dossier store unavailable: {exc}") from exc


@app.post(
    "/sales/prospect/deep-research",
    response_model=DeepResearchResult,
    tags=["prospecting"],
)
def deep_research(body: DeepResearchRequest, request: Request) -> DeepResearchResult:
    """Run the deep-research prospecting pipeline.

    Executes company → decision-maker → dossier in sequence and returns a
    ranked top-N list. Every entry references its dossier by ``dossier_id``
    and carries a ``dossier_url`` for direct retrieval. No company may appear
    more than ``max_per_company`` times in the list (default 2).
    """
    orchestrator = SalesPodOrchestrator()

    def _build_dossier_url(dossier_id: str) -> str:
        """Resolve the dossier URL against this app's actual registered route.

        Using ``request.url_for("get_dossier", ...)`` means the emitted URL
        tracks whatever path the route is mounted at — including the
        ``/api/sales`` prefix that the unified API adds — so clients can
        always follow the link without hard-coding a prefix.
        """
        try:
            return str(request.url_for("get_dossier", dossier_id=dossier_id))
        except Exception:
            # Fall back to the unified-api shape if url_for fails for any
            # reason (e.g. route name changes, routing context missing).
            return f"/api/sales/dossiers/{dossier_id}"

    return orchestrator.deep_research_only(body, dossier_url_builder=_build_dossier_url)


@app.get(
    "/sales/dossiers/{dossier_id}",
    response_model=ProspectDossier,
    tags=["prospecting"],
    name="get_dossier",
)
def get_dossier(dossier_id: str) -> ProspectDossier:
    """Return a single ProspectDossier by ID, or 404 if not found."""
    store = _load_dossier_store()
    try:
        dossier = store.get_dossier(dossier_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Dossier store unavailable: {exc}") from exc
    if dossier is None:
        raise HTTPException(status_code=404, detail=f"Dossier {dossier_id} not found")
    return dossier


@app.get(
    "/sales/prospect-lists/{list_id}",
    response_model=DeepResearchResult,
    tags=["prospecting"],
    name="get_prospect_list",
)
def get_prospect_list(list_id: str) -> DeepResearchResult:
    """Return a saved deep-research prospect list by ID, or 404 if not found."""
    store = _load_dossier_store()
    try:
        result = store.get_prospect_list(list_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Dossier store unavailable: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"Prospect list {list_id} not found")
    return result


@app.get("/sales/prospect-lists", tags=["prospecting"])
def list_prospect_lists(limit: int = 50) -> List[Dict[str, Any]]:
    """Return lightweight summaries of recent deep-research prospect lists."""
    store = _load_dossier_store()
    try:
        return store.list_prospect_lists(limit=max(1, min(limit, 200)))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Dossier store unavailable: {exc}") from exc


# ---------------------------------------------------------------------------
# Outcome recording endpoints
# ---------------------------------------------------------------------------


class RecordOutcomeResponse(BaseModel):
    outcome_id: str
    message: str


@app.post("/sales/outcomes/stage", response_model=RecordOutcomeResponse, tags=["learning"])
def record_stage_outcome_endpoint(request: RecordStageOutcomeRequest) -> RecordOutcomeResponse:
    """Record the outcome of a single pipeline stage for a prospect.

    Use this endpoint to feed real-world results back into the learning loop.
    Examples:
    - Outreach email got a reply → stage=outreach, outcome=converted
    - Prospect raised a price objection → stage=negotiation, outcome=objection
    - Lead went cold after discovery → stage=discovery, outcome=stalled

    The learning engine uses these records to improve future pipeline runs.
    """
    outcome = StageOutcome(
        pipeline_job_id=request.pipeline_job_id,
        company_name=request.company_name,
        industry=request.industry,
        stage=request.stage,
        outcome=request.outcome,
        icp_match_score=request.icp_match_score,
        qualification_score=request.qualification_score,
        email_touch_number=request.email_touch_number,
        subject_line_used=request.subject_line_used,
        objection_text=request.objection_text,
        close_technique_used=request.close_technique_used,
        notes=request.notes,
    )
    saved = record_stage_outcome(outcome)
    return RecordOutcomeResponse(
        outcome_id=saved.outcome_id,
        message=f"Stage outcome recorded for {request.company_name} @ {request.stage.value}.",
    )


@app.post("/sales/outcomes/deal", response_model=RecordOutcomeResponse, tags=["learning"])
def record_deal_outcome_endpoint(request: RecordDealOutcomeRequest) -> RecordOutcomeResponse:
    """Record the final outcome of a deal (won or lost).

    This is the highest-signal feedback for the learning engine. Always record
    a deal outcome when a deal closes — win or loss.

    The more deal outcomes you record, the more precisely the learning engine
    can identify winning vs. losing patterns and adapt agent behavior.
    """
    outcome = DealOutcome(
        pipeline_job_id=request.pipeline_job_id,
        company_name=request.company_name,
        industry=request.industry,
        deal_size_usd=request.deal_size_usd,
        final_stage_reached=request.final_stage_reached,
        result=request.result,
        loss_reason=request.loss_reason,
        win_factor=request.win_factor,
        close_technique_used=request.close_technique_used,
        objections_raised=request.objections_raised,
        stages_completed=request.stages_completed,
        icp_match_score=request.icp_match_score,
        qualification_score=request.qualification_score,
        sales_cycle_days=request.sales_cycle_days,
        notes=request.notes,
    )
    saved = record_deal_outcome(outcome)
    return RecordOutcomeResponse(
        outcome_id=saved.outcome_id,
        message=f"Deal outcome ({request.result.value}) recorded for {request.company_name}.",
    )


@app.get("/sales/outcomes/summary", tags=["learning"])
def get_outcome_summary() -> Dict[str, Any]:
    """Return a count of recorded outcomes and whether insights have been generated."""
    return outcome_counts()


@app.get("/sales/outcomes/stage", response_model=List[StageOutcome], tags=["learning"])
def list_stage_outcomes(limit: int = 100) -> List[StageOutcome]:
    """List recorded stage outcomes (newest first, up to limit)."""
    return load_stage_outcomes(limit=min(limit, 500))


@app.get("/sales/outcomes/deal", response_model=List[DealOutcome], tags=["learning"])
def list_deal_outcomes(limit: int = 100) -> List[DealOutcome]:
    """List recorded deal outcomes (newest first, up to limit)."""
    return load_deal_outcomes(limit=min(limit, 500))


# ---------------------------------------------------------------------------
# Learning insights endpoints
# ---------------------------------------------------------------------------


@app.get("/sales/insights", response_model=LearningInsights, tags=["learning"])
def get_insights() -> LearningInsights:
    """Return the current LearningInsights snapshot.

    Returns 404 if no outcomes have been recorded yet.
    Call POST /sales/insights/refresh to generate or update insights.
    """
    insights = load_current_insights()
    if not insights:
        raise HTTPException(
            status_code=404,
            detail=(
                "No learning insights available yet. "
                "Record outcomes via POST /sales/outcomes/stage or /sales/outcomes/deal, "
                "then call POST /sales/insights/refresh."
            ),
        )
    return insights


class InsightsRefreshResponse(BaseModel):
    message: str
    insights_version: int
    total_outcomes_analyzed: int
    win_rate: float


@app.post("/sales/insights/refresh", response_model=InsightsRefreshResponse, tags=["learning"])
def refresh_insights() -> InsightsRefreshResponse:
    """Trigger a learning engine run to regenerate insights from all recorded outcomes.

    This runs synchronously (may take a few seconds with the Strands SDK).
    The updated insights are immediately applied to the next pipeline run.

    Tip: call this endpoint after recording a batch of outcomes (e.g. end-of-week
    pipeline review) to keep the learning loop current.
    """
    engine = LearningEngine()
    insights = engine.refresh()
    return InsightsRefreshResponse(
        message=(
            f"Insights refreshed to v{insights.insights_version}. "
            f"Analyzed {insights.total_outcomes_analyzed} outcomes. "
            "All future pipeline runs will use these updated patterns."
        ),
        insights_version=insights.insights_version,
        total_outcomes_analyzed=insights.total_outcomes_analyzed,
        win_rate=insights.win_rate,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"])
def health() -> Dict[str, str]:
    counts = outcome_counts()
    return {
        "status": "ok",
        "stage_outcomes_recorded": str(counts["stage_outcomes"]),
        "deal_outcomes_recorded": str(counts["deal_outcomes"]),
        "insights_available": str(counts["has_insights"]),
    }
