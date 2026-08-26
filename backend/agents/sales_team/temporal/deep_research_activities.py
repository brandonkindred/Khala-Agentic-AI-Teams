"""Temporal activities for the deep-research prospecting pipeline.

``DeepResearchWorkflow`` runs company → decision-maker → dossier as durable
activities, fanning out one activity per company (decision-maker mapping) and
one per prospect (dossier building) via ``asyncio.gather`` in place of the
thread pools of the synchronous ``deep_research_only`` path. The per-item
activities delegate to the shared ``SalesPodOrchestrator.map_company_one`` /
``build_dossier_one`` methods, so the exact same agent-call logic runs in both
modes.

The retry-safe job-store contract and terminal-state semantics match the main
pipeline's activities (``sales_team.temporal.activities``) and reuse its
helpers: activities never write FAILED (the workflow's catch-all does, via
``sales_mark_failed``); a clean terminal short-circuits while a FAILED/missing
job at prepare/finalize raises.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sales_team.temporal.activities import (
    _beating,
    _GuardOutcome,
    _job_stopped,
    _terminal_guard,
)
from sales_team.temporal.phase_models import DeepResearchContext


def _deep_orch():
    """Construct a default-config orchestrator for a deep-research activity.

    Preconditions:
        - Called inside an activity body (does the lazy heavy import there).
    Postconditions:
        - Returns a ``SalesPodOrchestrator`` with default config; only the
          agents an activity actually invokes resolve an LLM client (agents are
          lazy properties). Deep research uses no thread-pool config, so the
          default config is correct.
    """
    from sales_team.orchestrator import SalesPodOrchestrator

    return SalesPodOrchestrator()


@activity.defn(name="deep_research_prepare")
def prepare_deep_research_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Open the run: validate the request, guard terminal state, load insights.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``DeepResearchRequest``.

    Postconditions:
        - Invalid ``request`` → non-retryable ``ApplicationError`` (the
          workflow's catch-all records FAILED).
        - Job missing → raises (retryable); job already FAILED → raises
          non-retryably; a clean terminal (cancelled/interrupted/completed) →
          returns ``stopped=True`` without writing RUNNING.
        - Otherwise writes RUNNING, loads insights once, and returns the
          ``DeepResearchContext`` carrier (with ``icp_json`` and
          ``companies_requested`` precomputed).
    """
    from job_service_client import JOB_STATUS_RUNNING
    from sales_team.job_runner import job_manager
    from sales_team.learning_engine import format_insights_for_prompt
    from sales_team.models import DeepResearchRequest
    from sales_team.outcome_store import load_current_insights

    try:
        dr_request = DeepResearchRequest(**request)
    except Exception as exc:
        raise ApplicationError(
            f"Invalid DeepResearchRequest for job {job_id}: {exc}", non_retryable=True
        ) from exc

    guard = _terminal_guard(
        job_id,
        phase="deep_research_prepare",
        missing_msg=f"Deep-research job {job_id} not found at prepare",
    )
    if guard is _GuardOutcome.STOP:
        return DeepResearchContext(request=dr_request, job_id=job_id, stopped=True).model_dump(
            mode="json"
        )

    job_manager.update_job(
        job_id,
        status=JOB_STATUS_RUNNING,
        current_stage="initializing",
        progress=2,
        eta_hint="Starting deep research...",
    )

    return DeepResearchContext(
        request=dr_request,
        job_id=job_id,
        insights_ctx=format_insights_for_prompt(load_current_insights()),
        icp_json=dr_request.icp.model_dump_json(indent=2),
        # Over-request companies so dedupe, failures, and the per-company cap
        # still leave enough prospects to hit the target (mirrors the sync path).
        companies_requested=min(100, max(40, dr_request.target_prospects)),
    ).model_dump(mode="json")


@activity.defn(name="deep_research_companies")
def companies_activity(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Produce the company shortlist. Single-shot; fatal on error.

    Postconditions:
        - Job terminal/missing → returns ``[]`` (the workflow finalizes with a
          "no companies" note).
        - Otherwise returns the company prospect dicts. A genuine failure
          propagates unmarked so the workflow's retry re-runs it.
    """
    dctx = DeepResearchContext.model_validate(ctx)
    if _job_stopped(dctx.job_id):
        return []
    try:
        orch = _deep_orch()
        req = dctx.request
        with _beating():
            result = orch.prospector.prospect_companies(
                dctx.icp_json,
                req.product_name,
                req.value_proposition,
                dctx.companies_requested,
                req.company_context,
                dctx.insights_ctx,
            )
        return [c.model_dump(mode="json") for c in result.prospects]
    except Exception:
        activity.logger.exception("deep_research_companies failed for job %s", dctx.job_id)
        raise


@activity.defn(name="deep_research_map_company_one")
def map_company_one_activity(ctx: dict[str, Any], company: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one company's decision-makers into ``{"prospect", "confidence"}`` dicts.

    Per-company fan-out. Terminal job → non-retryable skip; other failures
    re-raise for Temporal's retry (the workflow drops a company that fails
    after its retries).
    """
    from sales_team.models import Prospect

    dctx = DeepResearchContext.model_validate(ctx)
    company_obj = Prospect.model_validate(company)
    if _job_stopped(dctx.job_id):
        raise ApplicationError(
            f"Deep-research job {dctx.job_id} is terminal; skipping company map",
            non_retryable=True,
        )
    try:
        orch = _deep_orch()
        req = dctx.request
        with _beating():
            entries = orch.map_company_one(
                company_obj,
                dctx.icp_json,
                req.product_name,
                req.value_proposition,
                req.max_per_company,
                dctx.insights_ctx,
            )
        return [{"prospect": p.model_dump(mode="json"), "confidence": conf} for p, conf in entries]
    except ApplicationError:
        raise
    except Exception:
        activity.logger.exception(
            "deep_research_map_company_one failed company=%s", company_obj.company_name
        )
        raise


@activity.defn(name="deep_research_rank")
def rank_activity(ctx: dict[str, Any], mapped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cap per company, rank, trim, and assign prospect ids. Single-shot.

    Preconditions:
        - ``mapped`` is the flattened decision-maker output
          (``{"prospect", "confidence"}`` dicts).

    Postconditions:
        - Job terminal/missing → returns ``[]``.
        - Otherwise returns the ranked prospect dicts, each with a stable id.
    """
    from sales_team.models import Prospect
    from sales_team.orchestrator import rank_and_assign_ids

    dctx = DeepResearchContext.model_validate(ctx)
    if _job_stopped(dctx.job_id):
        return []
    pairs = [(Prospect.model_validate(m["prospect"]), m["confidence"]) for m in mapped]
    final = rank_and_assign_ids(pairs, dctx.request.max_per_company, dctx.request.target_prospects)
    return [p.model_dump(mode="json") for p in final]


@activity.defn(name="deep_research_build_dossier_one")
def build_dossier_one_activity(ctx: dict[str, Any], prospect: dict[str, Any]) -> dict[str, Any]:
    """Build one prospect's dossier. Per-prospect fan-out.

    Terminal job → non-retryable skip; other failures re-raise for retry (the
    workflow drops a prospect whose dossier fails, matching the sync path).
    """
    from sales_team.models import Prospect

    dctx = DeepResearchContext.model_validate(ctx)
    p = Prospect.model_validate(prospect)
    if _job_stopped(dctx.job_id):
        raise ApplicationError(
            f"Deep-research job {dctx.job_id} is terminal; skipping dossier build",
            non_retryable=True,
        )
    try:
        orch = _deep_orch()
        req = dctx.request
        with _beating():
            dossier = orch.build_dossier_one(
                p, req.product_name, req.value_proposition, dctx.insights_ctx
            )
        return dossier.model_dump(mode="json")
    except ApplicationError:
        raise
    except Exception:
        activity.logger.exception("deep_research_build_dossier_one failed prospect_id=%s", p.id)
        raise


@activity.defn(name="deep_research_finalize")
def finalize_deep_research_activity(
    ctx: dict[str, Any],
    final_prospects: list[dict[str, Any]],
    dossiers: list[dict[str, Any]],
    extra_notes: list[str],
) -> dict[str, Any]:
    """Assemble + persist the ranked result and write COMPLETED.

    The only writer of COMPLETED for a deep-research run.

    Preconditions:
        - ``final_prospects`` are the ranked prospect dicts (empty for an
          early-exit); ``dossiers`` are the built dossier dicts (keyed to
          prospects by their ``prospect_id``).

    Postconditions:
        - Clean terminal → returns without writing (cancel wins / idempotent
          replay); FAILED → raises non-retryably; missing → raises.
        - Otherwise persists dossiers + the list (best-effort) and writes
          COMPLETED with the ``DeepResearchResult``.
    """
    from sales_team.job_runner import write_job_completed
    from sales_team.models import Prospect, ProspectDossier
    from sales_team.orchestrator import assemble_and_persist_deep_research

    dctx = DeepResearchContext.model_validate(ctx)
    job_id = dctx.job_id
    missing_msg = f"Deep-research job {job_id} not found at finalize"

    if (
        _terminal_guard(job_id, phase="deep_research_finalize", missing_msg=missing_msg)
        is _GuardOutcome.STOP
    ):
        return {"job_id": job_id}

    prospects = [Prospect.model_validate(p) for p in final_prospects]
    dossier_map = {
        d["prospect_id"]: ProspectDossier.model_validate(d)
        for d in dossiers
        if d.get("prospect_id")
    }
    result = assemble_and_persist_deep_research(
        product_name=dctx.request.product_name,
        final_prospects=prospects,
        dossiers=dossier_map,
        extra_notes=list(extra_notes),
        target_prospects=dctx.request.target_prospects,
        dossier_url_builder=None,  # async path: no request scope → default URL shape
        persist=True,
    )

    if (
        _terminal_guard(job_id, phase="deep_research_finalize", missing_msg=missing_msg)
        is _GuardOutcome.STOP
    ):
        return {"job_id": job_id}

    write_job_completed(job_id, result.model_dump())
    return {"job_id": job_id}
