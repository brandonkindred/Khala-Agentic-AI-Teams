"""Temporal activities for the sales_team — one per specialist agent invocation.

The fine-grained ``SalesWorkflow`` fans out **one activity per prospect per
stage** (outreach, qualification, nurture, discovery, proposal, negotiation),
plus a handful of single-shot activities (prepare, prospect, load-dossiers,
coach, progress, finalize). Each per-prospect activity delegates to the shared
``SalesPodOrchestrator.<stage>_one`` method, so the exact same agent-call logic
runs in both the thread path (bounded thread pool) and Temporal mode (durable,
individually retryable activities) — there is no second implementation to drift.

Import hygiene (mirrors the previous single activity + ``test_temporal_bootstrap``):
top-level imports are light (``temporalio``, stdlib, ``shared_concurrency``,
``phase_models``); every heavy import (``orchestrator``, ``job_runner``,
``models``, ``outcome_store``) is lazy inside a function body, and ``os.getenv``
is only ever called inside a body — never at import — so the temporalio workflow
sandbox that re-imports sibling modules is never tripped.

Job-store status ownership:
    - RUNNING is written once, by ``prepare_sales_pipeline_activity``.
    - Per-stage progress is written by ``report_progress_activity`` at each
      stage boundary (which also reports whether the job is still active, so the
      workflow can stop spending on a cancelled job).
    - COMPLETED is written only by ``finalize_sales_pipeline_activity``.
    - FAILED is written by the *fatal* single-shot activities on error
      (prepare, prospect, finalize). A single prospect's activity failure is
      NOT fatal — it re-raises so Temporal retries and, if terminal, the
      workflow drops just that prospect (matching the thread path's skip).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from temporalio import activity

from sales_team.temporal.phase_models import SalesRunContext
from shared_concurrency import BackgroundHeartbeat

_DEFAULT_HEARTBEAT_INTERVAL_S = 30.0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _heartbeat_interval_s() -> float:
    """Heartbeat cadence (seconds) for the long LLM activities.

    Postconditions: returns ``SALES_TEMPORAL_HEARTBEAT_INTERVAL_S`` when it
    parses to a positive float, else the 30s default (garbage/≤0 → default).
    """
    raw = os.getenv("SALES_TEMPORAL_HEARTBEAT_INTERVAL_S", "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_HEARTBEAT_INTERVAL_S
    return val if val > 0 else _DEFAULT_HEARTBEAT_INTERVAL_S


def _beating() -> BackgroundHeartbeat:
    """Background beater keeping a long LLM activity alive across blocking calls.

    Used as a context manager. ``copy_context=True`` carries the Temporal
    activity handle into the beater thread; beat errors outside an activity
    context (e.g. direct calls in unit tests) are swallowed so the loop
    survives.
    """
    return BackgroundHeartbeat(
        activity.heartbeat,
        _heartbeat_interval_s(),
        name="sales-heartbeat",
        copy_context=True,
        join_timeout=5.0,
    )


def _job_is_terminal(job_id: str) -> bool:
    """Whether ``job_id`` is already in a terminal state (or missing).

    A missing job is treated as terminal — a queued activity must not run work
    for a job that no longer exists. Used to short-circuit stage activities so a
    cancel/interrupt lands mid-run stops further LLM spend.
    """
    from sales_team.job_runner import _TERMINAL_STATUSES, job_manager

    job = job_manager.get_job(job_id)
    return job is None or job.get("status") in _TERMINAL_STATUSES


def _fail(job_id: str, exc: Exception) -> None:
    """Best-effort mark ``job_id`` FAILED — mirrors ``run_pipeline_job``'s write.

    Never raises: a failing job-store update must not mask the original error
    that the caller is about to re-raise.
    """
    from job_service_client import JOB_STATUS_FAILED
    from sales_team.job_runner import _now, job_manager

    try:
        job_manager.update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            current_stage="failed",
            error=str(exc),
            eta_hint=None,
            last_updated_at=_now(),
        )
    except Exception:
        activity.logger.exception("Failed to mark sales job %s FAILED", job_id)


def _orch_and_ctx(sctx: SalesRunContext):
    """Reconstruct a fresh orchestrator + ``_RunContext`` from the carrier.

    Progress is reported out-of-band (``report_progress_activity``), so the
    run-context uses the no-op update callback — the non-serializable callback
    never has to cross a Temporal boundary.
    """
    from sales_team.orchestrator import SalesPodOrchestrator, build_run_context

    orch = SalesPodOrchestrator(config=sctx.request.config)
    run_ctx = build_run_context(sctx.request, sctx.job_id, sctx.insights_ctx)
    return orch, run_ctx


# ---------------------------------------------------------------------------
# Single-shot activities
# ---------------------------------------------------------------------------


@activity.defn(name="sales_prepare")
def prepare_sales_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Open the run: validate the request, guard terminal state, load insights.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``SalesPipelineRequest``.

    Postconditions:
        - Invalid ``request`` → job marked FAILED and the validation error
          re-raised (never leaves the job stuck PENDING).
        - Job missing/terminal at start → returns a ``SalesRunContext`` with
          ``stopped=True`` and RUNNING is NOT written (a queued workflow cannot
          resurrect a cancelled job).
        - Otherwise writes RUNNING, loads learning insights once, and returns
          the ``SalesRunContext`` carrier (as a dict) for every downstream
          activity.
    """
    from job_service_client import JOB_STATUS_RUNNING
    from sales_team.job_runner import _TERMINAL_STATUSES, job_manager
    from sales_team.learning_engine import format_insights_for_prompt
    from sales_team.models import SalesPipelineRequest
    from sales_team.outcome_store import load_current_insights

    try:
        pipeline_request = SalesPipelineRequest(**request)
    except Exception as exc:
        _fail(job_id, exc)
        raise

    existing = job_manager.get_job(job_id)
    if existing is None or existing.get("status") in _TERMINAL_STATUSES:
        activity.logger.info(
            "Sales job %s missing/terminal at prepare (%s); stopping run",
            job_id,
            (existing or {}).get("status"),
        )
        return SalesRunContext(request=pipeline_request, job_id=job_id, stopped=True).model_dump(
            mode="json"
        )

    job_manager.update_job(
        job_id,
        status=JOB_STATUS_RUNNING,
        current_stage="initializing",
        progress=2,
        eta_hint="Starting pipeline...",
    )

    insights = load_current_insights()
    return SalesRunContext(
        request=pipeline_request,
        job_id=job_id,
        insights_ctx=format_insights_for_prompt(insights),
        insights_version=insights.insights_version if insights else None,
        insights_total_outcomes=insights.total_outcomes_analyzed if insights else 0,
    ).model_dump(mode="json")


@activity.defn(name="sales_prospect")
def prospect_activity(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate (or adopt existing) prospects. Single-shot; fatal on error.

    Postconditions:
        - Job terminal → returns ``[]`` (the workflow early-exits to finalize).
        - Otherwise returns the prospect list as dicts. On a genuine failure the
          job is marked FAILED and the error re-raised (prospecting failure is
          fatal, matching the thread path where it propagates to run()).
    """
    sctx = SalesRunContext.model_validate(ctx)
    if _job_is_terminal(sctx.job_id):
        return []
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        with _beating():
            prospects = orch._run_prospecting(run_ctx)
        return [p.model_dump(mode="json") for p in prospects]
    except Exception as exc:
        activity.logger.exception("sales_prospect failed for job %s", sctx.job_id)
        _fail(sctx.job_id, exc)
        raise


@activity.defn(name="sales_load_dossiers")
def load_dossiers_activity(
    ctx: dict[str, Any], prospects: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Batch-load dossiers for ``prospects`` keyed by prospect id.

    Idempotent DB read; ``load_dossiers_for_prospects`` is itself failure-safe
    (returns ``{}`` when Postgres is unreachable), so this never raises and
    never fails the job.

    Postconditions: returns ``{prospect_id: dossier_dict}`` for prospects that
    have a saved dossier (others absent), mirroring the thread path.
    """
    from sales_team.models import Prospect

    sctx = SalesRunContext.model_validate(ctx)
    orch, _ = _orch_and_ctx(sctx)
    dmap = orch.load_dossiers_for_prospects([Prospect.model_validate(p) for p in prospects])
    return {pid: d.model_dump(mode="json") for pid, d in dmap.items()}


@activity.defn(name="sales_coach")
def coach_activity(
    ctx: dict[str, Any], prospects: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Generate the pipeline coaching report over all prospects.

    Best-effort (matches the thread path's ``_run_coaching``): failures return
    ``None`` rather than failing the job.

    Postconditions: returns the coaching report dict, or ``None`` if coaching
    could not be produced.
    """
    from sales_team.models import Prospect

    sctx = SalesRunContext.model_validate(ctx)
    orch, run_ctx = _orch_and_ctx(sctx)
    with _beating():
        report = orch._run_coaching(run_ctx, [Prospect.model_validate(p) for p in prospects])
    return report.model_dump(mode="json") if report is not None else None


@activity.defn(name="sales_report_progress")
def report_progress_activity(job_id: str, stage: str, pct: int) -> bool:
    """Write stage progress and report whether the job is still active.

    Serves double duty so the workflow needs only one cheap job-store round-trip
    per stage boundary: it records ``current_stage``/``progress`` for the UI and
    returns ``False`` when the job has gone terminal, letting the workflow stop
    scheduling expensive fan-out work for a cancelled/interrupted job.

    Postconditions:
        - Job missing/terminal → returns ``False`` and writes nothing.
        - Otherwise writes progress and returns ``True``.
    """
    from sales_team.job_runner import _TERMINAL_STATUSES, _now, job_manager

    job = job_manager.get_job(job_id)
    if job is None or job.get("status") in _TERMINAL_STATUSES:
        return False
    job_manager.update_job(job_id, current_stage=stage, progress=pct, last_updated_at=_now())
    return True


@activity.defn(name="sales_finalize")
def finalize_sales_pipeline_activity(ctx: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Record outcomes, assemble the summary, and write COMPLETED.

    The only writer of COMPLETED. Preserves the thread path's terminal guard: a
    cancel/interrupt that lands before or during finalize is left in place
    (COMPLETED is never written over a terminal status).

    Postconditions:
        - Job terminal → returns ``{"job_id": ...}`` without writing COMPLETED
          or recording outcomes (a cancel wins).
        - No-prospects run → COMPLETED with the "Pipeline halted" summary.
        - Otherwise records prospecting outcomes, assembles the full summary,
          and writes COMPLETED with the result.
    """
    from job_service_client import JOB_STATUS_COMPLETED
    from sales_team.job_runner import _now, job_manager
    from sales_team.models import SalesPipelineResult

    sctx = SalesRunContext.model_validate(ctx)
    job_id = sctx.job_id
    try:
        if _job_is_terminal(job_id):
            activity.logger.info("Sales job %s terminal at finalize; not writing COMPLETED", job_id)
            return {"job_id": job_id}

        pipeline_result = SalesPipelineResult.model_validate(result)
        orch, _ = _orch_and_ctx(sctx)

        if not pipeline_result.prospects:
            pipeline_result.summary = "No prospects found or provided. Pipeline halted."
        else:
            orch._record_prospecting_outcomes(pipeline_result.prospects, job_id)
            insights_note = (
                f" (learning insights v{sctx.insights_version} applied)"
                if sctx.insights_total_outcomes > 0
                else " (no learning history yet — record outcomes to improve future runs)"
            )
            pipeline_result.summary = (
                f"Sales pod completed pipeline from '{pipeline_result.entry_stage.value}' "
                f"stage{insights_note}. "
                f"Prospects identified: {len(pipeline_result.prospects)}. "
                f"Outreach sequences generated: {len(pipeline_result.outreach_sequences)}. "
                f"Leads qualified: {len(pipeline_result.qualified_leads)}. "
                f"Nurture sequences: {len(pipeline_result.nurture_sequences)}. "
                f"Discovery plans: {len(pipeline_result.discovery_plans)}. "
                f"Proposals written: {len(pipeline_result.proposals)}. "
                f"Closing strategies: {len(pipeline_result.closing_strategies)}."
            )

        # A cancel can land while we assembled the summary; don't clobber it.
        if _job_is_terminal(job_id):
            activity.logger.info(
                "Sales job %s went terminal during finalize; not writing COMPLETED", job_id
            )
            return {"job_id": job_id}

        job_manager.update_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            current_stage="completed",
            progress=100,
            eta_hint="done",
            result=pipeline_result.model_dump(),
            last_updated_at=_now(),
        )
        return {"job_id": job_id}
    except Exception as exc:
        activity.logger.exception("sales_finalize failed for job %s", job_id)
        _fail(job_id, exc)
        raise


# ---------------------------------------------------------------------------
# Per-prospect fan-out activities
#
# Each delegates to the shared ``SalesPodOrchestrator.<stage>_one`` method and
# re-raises on error (after logging) so Temporal retries; the workflow's
# ``gather(return_exceptions=True)`` drops a prospect only after its retries are
# exhausted. None of these write job FAILED — a single prospect's failure is not
# fatal to the run (identical to the thread path's per-prospect skip).
# ---------------------------------------------------------------------------


@activity.defn(name="sales_outreach_one")
def outreach_one_activity(
    ctx: dict[str, Any], prospect: dict[str, Any], dossier: dict[str, Any]
) -> dict[str, Any]:
    """Generate one prospect's critic-gated outreach sequence.

    Preconditions: ``dossier`` is non-``None`` (the workflow only schedules this
    for prospects that have a dossier).
    """
    from sales_team.models import Prospect, ProspectDossier

    sctx = SalesRunContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        with _beating():
            seq = orch.outreach_one(p, ProspectDossier.model_validate(dossier), run_ctx)
        return seq.model_dump(mode="json")
    except Exception:
        activity.logger.exception("sales_outreach_one failed prospect_id=%s", p.id)
        raise


@activity.defn(name="sales_qualify_one")
def qualify_one_activity(ctx: dict[str, Any], prospect: dict[str, Any]) -> dict[str, Any]:
    """Qualify one prospect (BANT/MEDDIC)."""
    from sales_team.models import Prospect

    sctx = SalesRunContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        with _beating():
            score = orch.qualify_one(p, run_ctx)
        return score.model_dump(mode="json")
    except Exception:
        activity.logger.exception("sales_qualify_one failed prospect_id=%s", p.id)
        raise


@activity.defn(name="sales_nurture_one")
def nurture_one_activity(ctx: dict[str, Any], prospect: dict[str, Any]) -> dict[str, Any]:
    """Build one prospect's nurture sequence."""
    from sales_team.models import Prospect

    sctx = SalesRunContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        with _beating():
            seq = orch.nurture_one(p, run_ctx)
        return seq.model_dump(mode="json")
    except Exception:
        activity.logger.exception("sales_nurture_one failed prospect_id=%s", p.id)
        raise


@activity.defn(name="sales_discovery_one")
def discovery_one_activity(
    ctx: dict[str, Any], prospect: dict[str, Any], qual: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Prepare one prospect's discovery plan (``qual`` may be ``None``)."""
    from sales_team.models import Prospect, QualificationScore

    sctx = SalesRunContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        qual_obj = QualificationScore.model_validate(qual) if qual else None
        with _beating():
            plan = orch.discovery_one(p, qual_obj, run_ctx)
        return plan.model_dump(mode="json")
    except Exception:
        activity.logger.exception("sales_discovery_one failed prospect_id=%s", p.id)
        raise


@activity.defn(name="sales_proposal_one")
def proposal_one_activity(
    ctx: dict[str, Any],
    prospect: dict[str, Any],
    dossier: Optional[dict[str, Any]],
    qual: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Write one prospect's critic-gated proposal (``dossier``/``qual`` optional)."""
    from sales_team.models import Prospect, ProspectDossier, QualificationScore

    sctx = SalesRunContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        dossier_obj = ProspectDossier.model_validate(dossier) if dossier else None
        qual_obj = QualificationScore.model_validate(qual) if qual else None
        with _beating():
            proposal = orch.proposal_one(p, dossier_obj, qual_obj, run_ctx)
        return proposal.model_dump(mode="json")
    except Exception:
        activity.logger.exception("sales_proposal_one failed prospect_id=%s", p.id)
        raise


@activity.defn(name="sales_close_one")
def close_one_activity(
    ctx: dict[str, Any], prospect: dict[str, Any], proposal: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Develop one prospect's closing strategy (``proposal`` may be ``None``)."""
    from sales_team.models import Prospect, SalesProposal

    sctx = SalesRunContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    try:
        orch, run_ctx = _orch_and_ctx(sctx)
        proposal_obj = SalesProposal.model_validate(proposal) if proposal else None
        with _beating():
            strategy = orch.close_one(p, proposal_obj, run_ctx)
        return strategy.model_dump(mode="json")
    except Exception:
        activity.logger.exception("sales_close_one failed prospect_id=%s", p.id)
        raise
