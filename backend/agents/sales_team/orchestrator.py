"""SalesPodOrchestrator — coordinates all sales team agents through the pipeline."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from typing import Callable, List, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from shared.concurrency import parallel_map

from .agents import (
    CloserAgent,
    DecisionMakerMapperAgent,
    DiscoveryAgent,
    DossierBuilderAgent,
    LeadQualifierAgent,
    NurtureAgent,
    OutreachAgent,
    ProposalAgent,
    ProspectorAgent,
    SalesCoachAgent,
)
from .critics import OutreachCriticAgent, ProposalCriticAgent, format_critic_feedback
from .learning_engine import LearningEngine, format_insights_for_prompt
from .models import (
    PERSONALIZATION_CONFIDENCE_THRESHOLD,
    ClosingStrategy,
    DecisionMakerList,
    DeepResearchRequest,
    DeepResearchResult,
    DiscoveryPlan,
    EmailTouch,
    IdealCustomerProfile,
    LearningInsights,
    NurtureSequence,
    OutcomeResult,
    OutreachSequence,
    OutreachVariant,
    OutreachVariantList,
    PipelineCoachingReport,
    PipelineStage,
    ProposalRequest,
    Prospect,
    ProspectDossier,
    ProspectListEntry,
    QualificationScore,
    SalesPipelineConfig,
    SalesPipelineRequest,
    SalesPipelineResult,
    SalesProposal,
    StageOutcome,
)
from .outcome_store import load_current_insights, record_stage_outcome
from .routing import STAGE_PROGRESS, index_by_prospect_id, partition_qualified, stage_should_run

logger = logging.getLogger(__name__)

UpdateCallback = Callable[[str, int], None]

# Summary emitted when the pipeline halts with no prospects — shared verbatim
# by ``run()`` and the Temporal finalize activity.
NO_PROSPECTS_SUMMARY = "No prospects found or provided. Pipeline halted."

# Default length of a generated nurture sequence, in days. A 90-day (one quarter)
# cadence is the standard B2B nurture window; named here so it isn't an inline
# magic number at the call site and can be tuned in one place.
_DEFAULT_NURTURE_DURATION_DAYS = 90
# Default annual contract value used for proposal economics when a deal-specific
# figure isn't supplied. Placeholder pricing for the generated proposal; named
# here rather than buried as a literal so it's easy to find and override.
_DEFAULT_ANNUAL_COST = 25000.0


def _noop_update(_stage: str, _pct: int) -> None:
    """Default progress callback — does nothing."""


@dataclass
class _RunContext:
    """Pre-computed shared state threaded through each pipeline stage method.

    Built once inside ``run()`` from the inbound request and injected into
    every ``_run_*`` helper so stage methods stay pure-ish (they read ctx,
    call agents, and return results without referencing the request directly).
    """

    request: SalesPipelineRequest
    job_id: str
    icp_json: str
    product: str
    vp: str
    company_context: str
    cases: str
    entry: PipelineStage
    insights_ctx: str
    config: SalesPipelineConfig
    update: UpdateCallback


def build_run_context(
    request: SalesPipelineRequest,
    job_id: str,
    insights_ctx: str = "",
    update: Optional[UpdateCallback] = None,
) -> _RunContext:
    """Build the per-run context threaded through every stage helper.

    This is the single constructor for ``_RunContext``, shared by ``run()``
    (thread mode) and every Temporal per-stage activity, so both paths derive
    the exact same request-scoped fields (``icp_json``, ``cases``, ``entry`` …)
    and cannot drift.

    Preconditions:
        - ``request`` is a validated ``SalesPipelineRequest``.
        - ``job_id`` is the id of a job already created in the job store.

    Postconditions:
        - Returns a ``_RunContext`` whose fields are pure functions of
          ``request`` plus the supplied ``insights_ctx`` and ``update``.
        - ``update`` defaults to the no-op callback when omitted, so an
          activity that reports progress out-of-band passes nothing here.
    """
    return _RunContext(
        request=request,
        job_id=job_id,
        icp_json=request.icp.model_dump_json(indent=2),
        product=request.product_name,
        vp=request.value_proposition,
        company_context=request.company_context,
        cases="\n".join(request.case_study_snippets) if request.case_study_snippets else "",
        entry=request.entry_stage,
        insights_ctx=insights_ctx,
        config=request.config,
        update=update or _noop_update,
    )


# ---------------------------------------------------------------------------
# Orchestrator-only helpers
#
# The per-agent JSON parsing that used to live here is gone — agents now
# return typed Pydantic objects via ``llm_service.generate_structured``, and
# cross-model rules (citation verification, grade downgrade, confidence gate)
# are enforced inside model validators in ``models.py``. What's left here is
# *policy* that doesn't belong on the data itself:
#
#   - seeding decision-maker prospects with company-level context
#   - ranking + capping deep-research results
#   - emitting a fallback variant when a low-confidence dossier ends up with
#     zero surviving variants after model validation
# ---------------------------------------------------------------------------


def _decision_makers_to_entries(
    dm_list: DecisionMakerList, company: Prospect
) -> List[tuple[Prospect, float]]:
    """Inflate each DecisionMakerEntry into a full Prospect rooted in ``company``.

    Each returned tuple is ``(prospect, confidence)`` — the same shape the
    old ``_decision_makers_from_json`` produced — so the rest of the
    deep-research pipeline (ranking, capping) is unchanged.
    """
    results: List[tuple[Prospect, float]] = []
    for item in dm_list.contacts:
        name = (item.contact_name or "").strip()
        if not name:
            continue
        rationale = item.decision_maker_rationale or ""
        extra_notes = f"{rationale} (confidence: {item.confidence})".strip()
        base_notes = company.research_notes
        combined = (base_notes + "\n" + extra_notes).strip() if extra_notes else base_notes
        prospect = Prospect(
            company_name=company.company_name,
            website=company.website,
            contact_name=name,
            contact_title=item.contact_title or None,
            contact_email=None,  # never fabricate emails
            linkedin_url=item.linkedin_url,
            company_size_estimate=company.company_size_estimate,
            industry=company.industry,
            icp_match_score=company.icp_match_score,
            research_notes=combined,
            trigger_events=list(company.trigger_events or []),
        )
        results.append((prospect, item.confidence))
    return results


def _rank_score(entry: tuple[Prospect, float]) -> float:
    """Composite ranking score: 70% ICP fit, 30% decision-maker confidence."""
    prospect, confidence = entry
    return 0.7 * prospect.icp_match_score + 0.3 * confidence


def _enforce_cap_and_rank(
    entries: List[tuple[Prospect, float]],
    max_per_company: int,
    target_count: int,
) -> List[Prospect]:
    """Enforce the per-company cap, rank globally, and trim to ``target_count``.

    ``entries`` is a list of ``(prospect, confidence)`` pairs. Returns a
    plain ``List[Prospect]`` ordered by rank score descending.

    Rules:
    1. Drop duplicates by (company_name, linkedin_url or contact_name).
    2. For each company, keep only the top ``max_per_company`` contacts by
       their rank score.
    3. Sort the surviving list globally by rank score desc and trim to
       ``target_count``.
    """
    seen: set[tuple[str, str]] = set()
    deduped: List[tuple[Prospect, float]] = []
    for p, conf in entries:
        key = (
            (p.company_name or "").strip().lower(),
            (p.linkedin_url or p.contact_name or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append((p, conf))

    by_company: dict[str, List[tuple[Prospect, float]]] = {}
    for entry in deduped:
        p = entry[0]
        by_company.setdefault((p.company_name or "").strip().lower(), []).append(entry)

    capped: List[tuple[Prospect, float]] = []
    for company_list in by_company.values():
        company_list.sort(key=_rank_score, reverse=True)
        capped.extend(company_list[:max_per_company])

    capped.sort(key=_rank_score, reverse=True)
    return [entry[0] for entry in capped[:target_count]]


# ---------------------------------------------------------------------------
# Deep-research shared stages (module-level so the Temporal deep-research
# activities can reuse them without constructing a full orchestrator, and so
# the sync ``deep_research_only`` path and the async workflow stay single-source).
# ---------------------------------------------------------------------------


def default_dossier_url(dossier_id: str) -> str:
    """Fallback dossier URL matching the unified-api mount (``/api/sales``).

    Preconditions:
        - ``dossier_id`` is a non-empty dossier id.
    Postconditions:
        - Returns the relative retrieval path; used when no request-scoped
          ``url_for`` builder is available (the async job path and background
          thread path have no FastAPI request context).
    """
    return f"/api/sales/dossiers/{dossier_id}"


def rank_and_assign_ids(
    mapped: List[tuple[Prospect, float]], max_per_company: int, target_prospects: int
) -> List[Prospect]:
    """Cap per company, rank, trim to ``target_prospects``, and assign ids.

    Preconditions:
        - ``mapped`` is the list of ``(prospect, confidence)`` pairs from the
          decision-maker stage.
    Postconditions:
        - Returns the ranked prospects (≤ ``target_prospects``); every returned
          prospect has a non-empty ``id`` (a fresh ``prs_<uuid12>`` is minted
          for any that lacked one) so dossiers can reference them.
    """
    final = _enforce_cap_and_rank(mapped, max_per_company, target_prospects)
    for p in final:
        if not p.id:
            p.id = f"prs_{uuid4().hex[:12]}"
    return final


def assemble_and_persist_deep_research(
    *,
    product_name: str,
    final_prospects: List[Prospect],
    dossiers: dict[str, ProspectDossier],
    extra_notes: List[str],
    target_prospects: int,
    dossier_url_builder: Optional[Callable[[str], str]] = None,
    persist: bool = True,
) -> DeepResearchResult:
    """Assemble the ranked result, best-effort persisting dossiers + the list.

    The single Stage-5 implementation, shared by ``deep_research_only`` (sync)
    and the Temporal finalize activity (async).

    Preconditions:
        - ``dossiers`` is keyed by ``prospect.id``; ``final_prospects`` is the
          ranked list (possibly empty for an early-exit — no companies / no
          decision-makers).
    Postconditions:
        - Returns a ``DeepResearchResult`` whose entries are the ranked
          prospects that produced a dossier; prospects without one are dropped
          with a note. A shortfall note is added when a non-empty ranked list
          is shorter than ``target_prospects``.
        - When ``persist`` and there is at least one entry, dossiers and the
          list are saved via ``DossierStore`` (best-effort; a store outage is
          logged, not raised). An empty result is never persisted.
    """
    build_url = dossier_url_builder or default_dossier_url
    run_notes = list(extra_notes)
    if final_prospects and len(final_prospects) < target_prospects:
        run_notes.append(
            f"Only {len(final_prospects)} qualifying prospects after per-company cap "
            f"(target was {target_prospects})."
        )

    store = None
    if persist and final_prospects:
        try:
            from .dossier_store import DossierStore

            store = DossierStore()
        except Exception:
            logger.warning("DossierStore unavailable; continuing without persistence")
            store = None

    entries: List[ProspectListEntry] = []
    rank = 0
    for p in final_prospects:
        dossier = dossiers.get(p.id)
        if dossier is None:
            run_notes.append(f"No dossier produced for prospect {p.id} ({p.contact_name}).")
            continue
        if store is not None:
            try:
                dossier = store.save_dossier(dossier)
            except Exception:
                logger.exception("Failed to persist dossier %s", dossier.dossier_id)
        p.dossier_id = dossier.dossier_id
        rank += 1
        entries.append(
            ProspectListEntry(
                rank=rank,
                prospect=p,
                dossier_id=dossier.dossier_id,
                dossier_url=build_url(dossier.dossier_id),
            )
        )

    result = DeepResearchResult(
        list_id="",
        product_name=product_name,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        total_prospects=len(entries),
        companies_represented=len({e.prospect.company_name for e in entries}),
        entries=entries,
        notes="; ".join(run_notes),
    )
    if store is not None and entries:
        try:
            result = store.save_prospect_list(result)
        except Exception:
            logger.exception("Failed to persist prospect list")
    return result


def _build_fallback_variant(prospect: Prospect) -> OutreachVariant:
    """Minimal company_soft_opener variant.

    Emitted by :func:`_wrap_outreach_sequence` when the model-validated
    variant list for a low-confidence prospect ends up empty — the
    confidence-gate validator dropped everything above the soft-opener tier.
    """
    opener = (
        f"Saw the work coming out of {prospect.company_name} — wanted to ask whether you're "
        "the right person to talk to about improvements in this area. Happy to share what "
        "we've seen at similar companies if useful."
    )
    return OutreachVariant(
        angle="company_soft_opener",
        email_sequence=[
            EmailTouch(
                day=1,
                subject_line=f"Quick question for {prospect.company_name}",
                body=opener,
                call_to_action="Are you open to a 15-minute call next week?",
            )
        ],
        rationale="Dossier confidence below threshold — using company-level soft opener.",
        personalization_grade="fallback",
    )


def _wrap_outreach_sequence(
    variants: OutreachVariantList,
    prospect: Prospect,
    dossier: ProspectDossier,
    confidence_threshold: float = PERSONALIZATION_CONFIDENCE_THRESHOLD,
) -> OutreachSequence:
    """Wrap a validated :class:`OutreachVariantList` into a full OutreachSequence.

    ``confidence_threshold`` is forwarded to the model validator via context
    so ``SalesPipelineConfig.dossier_confidence_threshold`` overrides take
    effect.
    """
    context = {"dossier_confidence_threshold": confidence_threshold}
    seq = OutreachSequence.model_validate(
        {
            "prospect": prospect,
            "dossier_id": dossier.dossier_id,
            "dossier_confidence": dossier.confidence,
            "variants": [v.model_dump() for v in variants.variants],
        },
        context=context,
    )
    if not seq.variants:
        logger.warning("sales.outreach.no_variants prospect_id=%s — emitting fallback", prospect.id)
        seq.variants = [_build_fallback_variant(prospect)]
    logger.info(
        "sales.outreach.generated prospect_id=%s dossier_id=%s variants_count=%d "
        "angles=%s grades=%s confidence=%.2f",
        prospect.id,
        dossier.dossier_id,
        len(seq.variants),
        [v.angle for v in seq.variants],
        [v.personalization_grade for v in seq.variants],
        dossier.confidence,
    )
    return seq


def load_dossiers_for_prospects(prospects: List[Prospect]) -> dict[str, ProspectDossier]:
    """Batch-load saved dossiers for *prospects*, keyed by prospect id.

    Module-level so the Temporal ``sales_load_dossiers`` activity (a pure DB
    read) can call it without constructing an orchestrator.

    Preconditions:
        - ``prospects`` is a list of ``Prospect`` (ids may be empty).
    Postconditions:
        - Returns ``{prospect_id: dossier}`` for prospects with a saved
          dossier; prospects without one (or with empty ids) are absent.
        - Never raises: a store outage returns ``{}`` (logged), so callers
          treat a missing map as "no personalization basis", not an error.
    """
    ids = [p.id for p in prospects if p.id]
    if not ids:
        return {}
    try:
        from .dossier_store import DossierStore

        return DossierStore().get_dossiers_by_prospect_ids(ids)
    except Exception as exc:
        logger.warning(
            "DossierStore unavailable for outreach dossier lookup — skipping all "
            "outreach for this run. Error: %s",
            exc,
        )
        return {}


def coach_review(
    prospects: List[Prospect],
    product_name: str,
    insights_ctx: Optional[str],
    coach: Optional[SalesCoachAgent] = None,
) -> Optional[PipelineCoachingReport]:
    """Generate the pipeline coaching report — best-effort.

    Module-level so the Temporal ``sales_coach`` activity can run coaching with
    only a ``SalesCoachAgent`` instead of the full orchestrator.

    Preconditions:
        - ``prospects`` is the full prospect list for the run.
    Postconditions:
        - Returns the report, or ``None`` on any coaching failure (logged) —
          coaching never fails a pipeline run in either execution mode.
    """
    prospects_json = json.dumps([p.model_dump() for p in prospects], indent=2)
    try:
        agent = coach if coach is not None else SalesCoachAgent()
        return agent.review(prospects_json, product_name, "", insights_ctx)
    except Exception:
        logger.exception("sales.coaching.failed")
        return None


def _prospecting_outcome_id(job_id: str, prospect: Prospect) -> str:
    """Deterministic outcome id for one prospect's prospecting outcome.

    Derived from the job + prospect identity so an at-least-once replay of the
    recording step overwrites the same outcome-store file instead of minting a
    duplicate (the store writes one file per ``outcome_id``).

    Postconditions:
        - Returns a stable UUID string for the same ``(job_id, prospect)``
          pair; distinct prospects in a job get distinct ids.
    """
    key = prospect.id or f"{prospect.company_name}:{prospect.contact_name or ''}"
    return str(uuid5(NAMESPACE_URL, f"sales-prospecting-outcome:{job_id}:{key}"))


def record_prospecting_outcomes(prospects: List[Prospect], job_id: str) -> None:
    """Auto-record each identified prospect as a PROSPECTING / CONVERTED outcome.

    Seeds the outcome store so the learning engine has data before manual
    deal outcomes arrive. Module-level so the Temporal finalize activity can
    record outcomes without constructing an orchestrator.

    Preconditions:
        - ``job_id`` identifies the pipeline run the prospects came from.
    Postconditions:
        - One outcome per prospect, written with a deterministic
          ``outcome_id`` so replays are idempotent (same file overwritten).
        - Never raises: individual write failures are logged and skipped.
    """
    for p in prospects:
        try:
            record_stage_outcome(
                StageOutcome(
                    outcome_id=_prospecting_outcome_id(job_id, p),
                    pipeline_job_id=job_id,
                    company_name=p.company_name,
                    industry=p.industry,
                    stage=PipelineStage.PROSPECTING,
                    outcome=OutcomeResult.CONVERTED,
                    icp_match_score=p.icp_match_score,
                )
            )
        except Exception as exc:
            logger.debug("Could not auto-record prospecting outcome: %s", exc)


def build_pipeline_summary(
    entry_stage_value: str,
    *,
    prospects: int,
    outreach_sequences: int,
    qualified_leads: int,
    nurture_sequences: int,
    discovery_plans: int,
    proposals: int,
    closing_strategies: int,
    insights_version: Optional[int],
    insights_total_outcomes: int,
) -> str:
    """Assemble the completion summary string for a pipeline run.

    The single source of the user-facing summary — shared verbatim by the
    thread path (``run()``) and the Temporal finalize activity so the two
    modes can never report differently.

    Preconditions:
        - Counts are the lengths of the corresponding result collections.
    Postconditions:
        - Returns the summary, including the learning-insights note when
          ``insights_total_outcomes > 0``.
    """
    insights_note = (
        f" (learning insights v{insights_version} applied)"
        if insights_total_outcomes > 0
        else " (no learning history yet — record outcomes to improve future runs)"
    )
    return (
        f"Sales pod completed pipeline from '{entry_stage_value}' stage{insights_note}. "
        f"Prospects identified: {prospects}. "
        f"Outreach sequences generated: {outreach_sequences}. "
        f"Leads qualified: {qualified_leads}. "
        f"Nurture sequences: {nurture_sequences}. "
        f"Discovery plans: {discovery_plans}. "
        f"Proposals written: {proposals}. "
        f"Closing strategies: {closing_strategies}."
    )


class SalesPodOrchestrator:
    """Coordinates all sales pod agents through the full pipeline.

    Stages run sequentially from the requested entry point. Each stage's
    output is passed as context to the next stage.

    On each run, current LearningInsights are loaded from the outcome store
    and injected into every agent prompt so the pod continuously improves
    based on historical win/loss data.
    """

    def __init__(self, config: Optional[SalesPipelineConfig] = None) -> None:
        self.config = config or SalesPipelineConfig()

    # Specialists are lazy ``cached_property``s rather than eager ``__init__``
    # constructions: each agent resolves an LLM client at construction, and the
    # Temporal per-prospect activities build a fresh orchestrator per call —
    # eager construction would resolve all 13 clients to use one. Instances
    # remain assignable (tests inject mocks) because ``cached_property`` is a
    # non-data descriptor: instance ``__dict__`` entries take precedence.

    @cached_property
    def prospector(self) -> ProspectorAgent:
        return ProspectorAgent()

    @cached_property
    def outreach(self) -> OutreachAgent:
        return OutreachAgent()

    @cached_property
    def qualifier(self) -> LeadQualifierAgent:
        return LeadQualifierAgent()

    @cached_property
    def nurture(self) -> NurtureAgent:
        return NurtureAgent()

    @cached_property
    def discovery(self) -> DiscoveryAgent:
        return DiscoveryAgent()

    @cached_property
    def proposal(self) -> ProposalAgent:
        return ProposalAgent()

    @cached_property
    def closer(self) -> CloserAgent:
        return CloserAgent()

    @cached_property
    def coach(self) -> SalesCoachAgent:
        return SalesCoachAgent()

    @cached_property
    def decision_maker_mapper(self) -> DecisionMakerMapperAgent:
        return DecisionMakerMapperAgent()

    @cached_property
    def dossier_builder(self) -> DossierBuilderAgent:
        return DossierBuilderAgent()

    @cached_property
    def learning_engine(self) -> LearningEngine:
        return LearningEngine()

    @cached_property
    def outreach_critic(self) -> OutreachCriticAgent:
        return OutreachCriticAgent()

    @cached_property
    def proposal_critic(self) -> ProposalCriticAgent:
        return ProposalCriticAgent()

    def _should_run(self, stage: PipelineStage, entry: PipelineStage) -> bool:
        return stage_should_run(stage, entry)

    # ------------------------------------------------------------------
    # Critic-gated emit helpers (configurable refinement budget)
    # ------------------------------------------------------------------

    def _generate_outreach_with_critic(
        self,
        prospect: Prospect,
        dossier: ProspectDossier,
        product_name: str,
        value_proposition: str,
        case_studies: str,
        company_context: str,
        insights_context: Optional[str],
        icp: Optional[IdealCustomerProfile],
        max_refinements: int = 1,
        confidence_threshold: Optional[float] = None,
    ) -> OutreachSequence:
        """Emit -> wrap -> critic -> on revise, re-emit up to *max_refinements - 1* times.

        The final review-budget iteration is always spent reviewing the current
        sequence, so the returned sequence is never an unchecked regeneration.
        """
        threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else self.config.dossier_confidence_threshold
        )
        variants = self.outreach.generate_sequence(
            prospect.model_dump_json(indent=2),
            dossier,
            product_name,
            value_proposition,
            case_studies,
            company_context,
            insights_context,
        )
        sequence = _wrap_outreach_sequence(
            variants, prospect, dossier, confidence_threshold=threshold
        )

        if icp is None or max_refinements < 1:
            return sequence

        refined_ctx = company_context or ""
        for attempt in range(max_refinements):
            report = self.outreach_critic.review(sequence, dossier, icp)
            if report.approved:
                return sequence

            feedback = format_critic_feedback(report.violations, report.notes)

            if attempt == max_refinements - 1:
                # No review budget left to check a regenerated draft — return
                # the sequence that was just reviewed rather than ship one
                # that was never checked.
                logger.info(
                    "sales.outreach.critic_rejected_budget_exhausted prospect_id=%s "
                    "violations=%d attempt=%d/%d",
                    prospect.id,
                    report.must_fix_count(),
                    attempt + 1,
                    max_refinements,
                )
                break

            logger.info(
                "sales.outreach.critic_revise prospect_id=%s violations=%d attempt=%d/%d",
                prospect.id,
                report.must_fix_count(),
                attempt + 1,
                max_refinements,
            )
            refined_ctx = refined_ctx + "\n\nReviewer feedback to address:\n" + feedback
            try:
                variants = self.outreach.generate_sequence(
                    prospect.model_dump_json(indent=2),
                    dossier,
                    product_name,
                    value_proposition,
                    case_studies,
                    refined_ctx,
                    insights_context,
                )
            except Exception:
                logger.exception(
                    "sales.outreach.refine_failed prospect_id=%s — keeping previous", prospect.id
                )
                return sequence
            sequence = _wrap_outreach_sequence(
                variants,
                prospect,
                dossier,
                confidence_threshold=threshold,
            )

        return sequence

    def _generate_proposal_with_critic(
        self,
        prospect: Prospect,
        product_name: str,
        value_proposition: str,
        annual_cost: float,
        discovery_notes: str,
        case_studies: str,
        company_context: str,
        insights_context: Optional[str],
        dossier: Optional[ProspectDossier],
        qualification: Optional[QualificationScore],
        max_refinements: int = 1,
    ) -> SalesProposal:
        """Emit -> wrap -> critic -> on revise, re-emit up to *max_refinements* times."""
        body = self.proposal.write(
            prospect.model_dump_json(indent=2),
            product_name,
            value_proposition,
            annual_cost,
            discovery_notes,
            case_studies,
            company_context,
            insights_context,
        )
        proposal = SalesProposal(prospect=prospect, **body.model_dump())

        refined_notes = discovery_notes or ""
        for attempt in range(max_refinements):
            report = self.proposal_critic.review(proposal, dossier, qualification)
            if report.approved:
                return proposal

            feedback = format_critic_feedback(report.violations, report.notes)
            logger.info(
                "sales.proposal.critic_revise prospect_id=%s violations=%d attempt=%d/%d",
                prospect.id,
                report.must_fix_count(),
                attempt + 1,
                max_refinements,
            )
            refined_notes = refined_notes + "\n\nReviewer feedback to address:\n" + feedback
            try:
                body = self.proposal.write(
                    prospect.model_dump_json(indent=2),
                    product_name,
                    value_proposition,
                    annual_cost,
                    refined_notes,
                    case_studies,
                    company_context,
                    insights_context,
                )
            except Exception:
                logger.exception(
                    "sales.proposal.refine_failed prospect_id=%s — keeping previous", prospect.id
                )
                return proposal
            proposal = SalesProposal(prospect=prospect, **body.model_dump())

        return proposal

    def load_dossiers_for_prospects(self, prospects: List[Prospect]) -> dict[str, ProspectDossier]:
        """Batch-load dossiers for the prospects we're about to run outreach on.

        Delegates to the module-level :func:`load_dossiers_for_prospects` (the
        single implementation, also used by the Temporal activity). Public so
        HTTP handlers can build the ``dossier_map`` argument for
        :meth:`outreach_only` by prospect id.

        Postconditions:
            - Returns ``{prospect_id: dossier}``; never raises (store outage
              returns ``{}``).
        """
        return load_dossiers_for_prospects(prospects)

    # ------------------------------------------------------------------
    # Per-stage methods — each owns a single pipeline phase
    # ------------------------------------------------------------------

    def _run_prospecting(self, ctx: _RunContext) -> List[Prospect]:
        ctx.update("prospecting", STAGE_PROGRESS["prospecting"][0])
        logger.info("Sales pod [%s]: prospecting stage", ctx.job_id)
        if ctx.request.existing_prospects:
            prospects = ctx.request.existing_prospects
        else:
            prospects_result = self.prospector.prospect(
                ctx.icp_json,
                ctx.product,
                ctx.vp,
                ctx.request.max_prospects,
                ctx.company_context,
                ctx.insights_ctx,
            )
            prospects = list(prospects_result.prospects)
        ctx.update("prospecting", STAGE_PROGRESS["prospecting"][1])
        return prospects

    def _map_prospects_parallel(
        self, prospects: List[Prospect], fn: "Callable[[Prospect], object]"
    ) -> list:
        """Run ``fn(prospect)`` concurrently across *prospects*, preserving order.

        Each pipeline stage makes one independent per-prospect LLM call; running
        them in a bounded pool turns the stage's wall-clock from the sum of the
        calls into roughly the slowest call. ``fn`` owns its own error handling
        and returns ``None`` to skip a prospect (so each stage keeps its specific
        log message), exactly as the previous sequential ``for`` loops did.

        Preconditions: ``fn`` is side-effect-safe to call from worker threads
            (the agents wrap the thread-safe LLM client) and never raises (it
            returns ``None`` on failure).
        Postconditions: returns the non-``None`` results in the SAME order as
            *prospects* — identical to the sequential loop, only concurrent. Each
            task runs inside a copy of the calling thread's context so the LLM
            attribution/request-id contextvars propagate to the workers (a raw
            ``ThreadPoolExecutor`` does not copy them; see ``llm_service.attribution``).
        """
        return parallel_map(prospects, fn, max_workers=self.config.pipeline_stage_workers)

    def outreach_one(
        self, p: Prospect, dossier: ProspectDossier, ctx: _RunContext
    ) -> OutreachSequence:
        """Generate one prospect's critic-gated outreach sequence.

        The per-prospect unit of outreach work, shared by the thread-pool
        fan-out (``_run_outreach``) and the ``sales_outreach_one`` Temporal
        activity. Raises on failure — callers decide whether to skip (thread
        mode returns ``None``; the activity re-raises so Temporal can retry).

        Preconditions:
            - ``dossier`` is the (non-``None``) dossier for ``p``; the caller
              handles the missing-dossier skip.

        Postconditions:
            - Returns a fully-wrapped ``OutreachSequence`` for ``p`` or raises.
        """
        return self._generate_outreach_with_critic(
            p,
            dossier,
            ctx.product,
            ctx.vp,
            ctx.cases,
            ctx.company_context,
            ctx.insights_ctx,
            ctx.request.icp,
            max_refinements=ctx.config.critic_max_refinements,
            confidence_threshold=ctx.config.dossier_confidence_threshold,
        )

    def _run_outreach(
        self,
        ctx: _RunContext,
        prospects: List[Prospect],
    ) -> tuple[List[OutreachSequence], dict[str, ProspectDossier]]:
        ctx.update("outreach", STAGE_PROGRESS["outreach"][0])
        logger.info("Sales pod [%s]: outreach stage for %d prospects", ctx.job_id, len(prospects))
        dossier_map = self.load_dossiers_for_prospects(prospects)

        def _one(p: Prospect) -> Optional[OutreachSequence]:
            dossier = dossier_map.get(p.id)
            if dossier is None:
                logger.warning(
                    "sales.outreach.dossier_missing prospect_id=%s company=%s",
                    p.id,
                    p.company_name,
                )
                return None
            try:
                return self.outreach_one(p, dossier, ctx)
            except Exception:
                logger.exception(
                    "sales.outreach.failed prospect_id=%s company=%s",
                    p.id,
                    p.company_name,
                )
                return None

        sequences = self._map_prospects_parallel(prospects, _one)
        ctx.update("outreach", STAGE_PROGRESS["outreach"][1])
        return sequences, dossier_map

    def qualify_one(self, p: Prospect, ctx: _RunContext) -> QualificationScore:
        """Qualify one prospect (BANT/MEDDIC scoring). Raises on failure.

        Shared by ``_run_qualification`` and the ``sales_qualify_one`` activity.

        Postconditions:
            - Returns a ``QualificationScore`` whose ``prospect`` is ``p``.
        """
        body = self.qualifier.qualify(
            p.model_dump_json(indent=2),
            ctx.product,
            ctx.vp,
            "",
            ctx.insights_ctx,
        )
        return QualificationScore(prospect=p, **body.model_dump())

    def _run_qualification(
        self,
        ctx: _RunContext,
        prospects: List[Prospect],
    ) -> List[QualificationScore]:
        ctx.update("qualification", STAGE_PROGRESS["qualification"][0])
        logger.info("Sales pod [%s]: qualification stage", ctx.job_id)

        def _one(p: Prospect) -> Optional[QualificationScore]:
            try:
                return self.qualify_one(p, ctx)
            except Exception:
                logger.exception("sales.qualify.failed prospect_id=%s", p.id)
                return None

        qualified = self._map_prospects_parallel(prospects, _one)
        ctx.update("qualification", STAGE_PROGRESS["qualification"][1])
        return qualified

    def nurture_one(self, p: Prospect, ctx: _RunContext) -> NurtureSequence:
        """Build one prospect's nurture sequence. Raises on failure.

        Shared by ``_run_nurture`` and the ``sales_nurture_one`` activity.

        Postconditions:
            - Returns a ``NurtureSequence`` whose ``prospect`` is ``p``.
        """
        body = self.nurture.build_sequence(
            p.model_dump_json(indent=2),
            ctx.product,
            ctx.vp,
            _DEFAULT_NURTURE_DURATION_DAYS,
            ctx.insights_ctx,
        )
        return NurtureSequence(prospect=p, **body.model_dump())

    def _run_nurture(
        self,
        ctx: _RunContext,
        nurture_prospects: List[Prospect],
    ) -> List[NurtureSequence]:
        ctx.update("nurturing", STAGE_PROGRESS["nurturing"][0])
        logger.info("Sales pod [%s]: nurturing %d prospects", ctx.job_id, len(nurture_prospects))

        def _one(p: Prospect) -> Optional[NurtureSequence]:
            try:
                return self.nurture_one(p, ctx)
            except Exception:
                logger.exception("sales.nurture.failed prospect_id=%s", p.id)
                return None

        nurture_seqs = self._map_prospects_parallel(nurture_prospects, _one)
        ctx.update("nurturing", STAGE_PROGRESS["nurturing"][1])
        return nurture_seqs

    def discovery_one(
        self,
        p: Prospect,
        qual: Optional[QualificationScore],
        ctx: _RunContext,
        dossier: Optional[ProspectDossier] = None,
    ) -> DiscoveryPlan:
        """Prepare one prospect's discovery plan. Raises on failure.

        Shared by ``_run_discovery`` and the ``sales_discovery_one`` activity.

        Preconditions:
            - ``qual`` is the matching ``QualificationScore`` for ``p`` or
              ``None`` (an empty ``"{}"`` is passed to the agent in that case,
              preserving the prior behaviour).
            - ``dossier`` is the matching ``ProspectDossier`` for ``p`` or
              ``None`` when no dossier is saved; defaults to ``None`` so
              existing callers (e.g. the ``sales_discovery_one`` activity)
              keep working unmodified.

        Postconditions:
            - Returns a ``DiscoveryPlan`` whose ``prospect`` is ``p``.
            - ``dossier`` is forwarded to ``DiscoveryAgent.prepare`` as-is;
              ``None`` produces the same plan as before dossier support
              existed.
        """
        qual_json = qual.model_dump_json(indent=2) if qual else "{}"
        body = self.discovery.prepare(
            p.model_dump_json(indent=2),
            qual_json,
            ctx.product,
            ctx.vp,
            ctx.insights_ctx,
            dossier=dossier,
        )
        return DiscoveryPlan(prospect=p, **body.model_dump())

    def _run_discovery(
        self,
        ctx: _RunContext,
        qualified_prospects: List[Prospect],
        qualified: List[QualificationScore],
        dossier_map: dict[str, ProspectDossier],
    ) -> List[DiscoveryPlan]:
        """Run the discovery stage for all qualified prospects in parallel.

        Preconditions:
            - Every Prospect in ``qualified_prospects`` has a non-empty ``id``
              (guaranteed by ``_ensure_prospect_ids`` called before this stage).
            - ``qualified`` is a list of ``QualificationScore`` objects, typically a
              subset of all scored prospects from the qualification stage; entries
              without an ``id`` are silently excluded from the lookup dict.
            - ``dossier_map`` is ``{prospect_id: ProspectDossier}`` for prospects
              with a saved dossier, typically the map already resolved by
              ``_run_outreach``; if empty (e.g. the outreach stage was
              skipped), it is re-resolved here via
              ``load_dossiers_for_prospects``.

        Postconditions:
            - Returns a list whose length is ≤ ``len(qualified_prospects)``; any
              prospect for which ``discovery.prepare`` raises is excluded.
            - Each returned ``DiscoveryPlan.prospect.id`` matches the
              corresponding input prospect's ``id``.
            - If a prospect has no matching ``QualificationScore`` in ``qualified``,
              an empty JSON object (``"{}"``) is passed to ``discovery.prepare``,
              which is expected to handle this gracefully.
            - A prospect with no entry in the resolved ``dossier_map`` still
              produces a discovery plan (``dossier`` degrades to ``None``); this
              is not a new failure mode and never skips the stage.
        """
        ctx.update("discovery", STAGE_PROGRESS["discovery"][0])
        logger.info(
            "Sales pod [%s]: discovery stage for %d prospects",
            ctx.job_id,
            len(qualified_prospects),
        )
        qual_by_prospect_id = index_by_prospect_id(qualified, lambda q: q.prospect)
        if not dossier_map:
            dossier_map = self.load_dossiers_for_prospects(qualified_prospects)

        def _one(p: Prospect) -> Optional[DiscoveryPlan]:
            try:
                return self.discovery_one(
                    p, qual_by_prospect_id.get(p.id), ctx, dossier_map.get(p.id)
                )
            except Exception:
                logger.exception("sales.discovery.failed prospect_id=%s", p.id)
                return None

        plans = self._map_prospects_parallel(qualified_prospects, _one)
        ctx.update("discovery", STAGE_PROGRESS["discovery"][1])
        return plans

    def proposal_one(
        self,
        p: Prospect,
        dossier: Optional[ProspectDossier],
        qual: Optional[QualificationScore],
        ctx: _RunContext,
    ) -> SalesProposal:
        """Write one prospect's critic-gated proposal. Raises on failure.

        Shared by ``_run_proposal`` and the ``sales_proposal_one`` activity.
        Uses the default annual contract value (``_DEFAULT_ANNUAL_COST``) and an
        empty discovery-notes seed, matching the pre-decomposition behaviour.

        Postconditions:
            - Returns a ``SalesProposal`` whose ``prospect`` is ``p``.
        """
        return self._generate_proposal_with_critic(
            p,
            ctx.product,
            ctx.vp,
            _DEFAULT_ANNUAL_COST,
            "",
            ctx.cases,
            ctx.company_context,
            ctx.insights_ctx,
            dossier,
            qual,
            max_refinements=ctx.config.critic_max_refinements,
        )

    def _run_proposal(
        self,
        ctx: _RunContext,
        qualified_prospects: List[Prospect],
        qualified: List[QualificationScore],
        dossier_map: dict[str, ProspectDossier],
    ) -> List[SalesProposal]:
        ctx.update("proposal", STAGE_PROGRESS["proposal"][0])
        logger.info(
            "Sales pod [%s]: proposal stage for %d prospects",
            ctx.job_id,
            len(qualified_prospects),
        )
        qual_by_prospect_id = index_by_prospect_id(qualified, lambda q: q.prospect)
        if not dossier_map:
            dossier_map = self.load_dossiers_for_prospects(qualified_prospects)

        def _one(p: Prospect) -> Optional[SalesProposal]:
            try:
                return self.proposal_one(
                    p, dossier_map.get(p.id), qual_by_prospect_id.get(p.id), ctx
                )
            except Exception:
                logger.exception("sales.proposal.failed prospect_id=%s", p.id)
                return None

        proposals = self._map_prospects_parallel(qualified_prospects, _one)
        ctx.update("proposal", STAGE_PROGRESS["proposal"][1])
        return proposals

    def close_one(
        self, p: Prospect, proposal: Optional[SalesProposal], ctx: _RunContext
    ) -> ClosingStrategy:
        """Develop one prospect's closing strategy. Raises on failure.

        Shared by ``_run_negotiation`` and the ``sales_close_one`` activity.

        Preconditions:
            - ``proposal`` is the matching ``SalesProposal`` for ``p`` or
              ``None`` (an empty ``"{}"`` is passed to the agent in that case,
              preserving the prior behaviour).

        Postconditions:
            - Returns a ``ClosingStrategy`` whose ``prospect`` is ``p``.
        """
        prop_json = proposal.model_dump_json(indent=2) if proposal else "{}"
        body = self.closer.develop_strategy(
            p.model_dump_json(indent=2),
            prop_json,
            ctx.product,
            ctx.vp,
            ctx.insights_ctx,
        )
        return ClosingStrategy(prospect=p, **body.model_dump())

    def _run_negotiation(
        self,
        ctx: _RunContext,
        qualified_prospects: List[Prospect],
        proposals: List[SalesProposal],
    ) -> List[ClosingStrategy]:
        """Run the negotiation/closing stage for all qualified prospects in parallel.

        Preconditions:
            - Every Prospect in ``qualified_prospects`` has a non-empty ``id``
              (guaranteed by ``_ensure_prospect_ids`` called before this stage).
            - ``proposals`` is a list of ``SalesProposal`` objects; entries without
              an ``id`` on their embedded prospect are silently excluded from the
              lookup dict.

        Postconditions:
            - Returns a list whose length is ≤ ``len(qualified_prospects)``; any
              prospect for which ``closer.develop_strategy`` raises is excluded.
            - Each returned ``ClosingStrategy.prospect.id`` matches the
              corresponding input prospect's ``id``.
            - If a prospect has no matching ``SalesProposal`` in ``proposals``,
              an empty JSON object (``"{}"``) is passed to ``closer.develop_strategy``,
              which is expected to handle this gracefully.
        """
        ctx.update("negotiation", STAGE_PROGRESS["negotiation"][0])
        logger.info("Sales pod [%s]: closing strategy stage", ctx.job_id)
        proposal_by_prospect_id = index_by_prospect_id(proposals, lambda prop: prop.prospect)

        def _one(p: Prospect) -> Optional[ClosingStrategy]:
            try:
                return self.close_one(p, proposal_by_prospect_id.get(p.id), ctx)
            except Exception:
                logger.exception("sales.close.failed prospect_id=%s", p.id)
                return None

        strategies = self._map_prospects_parallel(qualified_prospects, _one)
        ctx.update("negotiation", STAGE_PROGRESS["negotiation"][1])
        return strategies

    def _run_coaching(
        self,
        ctx: _RunContext,
        prospects: List[Prospect],
    ) -> Optional[PipelineCoachingReport]:
        ctx.update("coaching", STAGE_PROGRESS["coaching"][0])
        logger.info("Sales pod [%s]: generating coaching report", ctx.job_id)
        return coach_review(prospects, ctx.product, ctx.insights_ctx, coach=self.coach)

    # ------------------------------------------------------------------
    # Pipeline driver — dispatches to stage methods and assembles result
    # ------------------------------------------------------------------

    def run(
        self,
        request: SalesPipelineRequest,
        job_id: str,
        update_cb: Optional[UpdateCallback] = None,
    ) -> SalesPipelineResult:
        current_insights: Optional[LearningInsights] = load_current_insights()
        insights_ctx: str = format_insights_for_prompt(current_insights)
        if current_insights and current_insights.total_outcomes_analyzed > 0:
            logger.info(
                "Sales pod [%s]: injecting learning insights v%d (%d outcomes, win_rate=%.0f%%)",
                job_id,
                current_insights.insights_version,
                current_insights.total_outcomes_analyzed,
                current_insights.win_rate * 100,
            )

        ctx = build_run_context(request, job_id, insights_ctx, update_cb)
        result = SalesPipelineResult(
            job_id=job_id,
            entry_stage=ctx.entry,
            product_name=ctx.product,
        )

        # Stage 1 — Prospecting
        if self._should_run(PipelineStage.PROSPECTING, ctx.entry):
            prospects = self._run_prospecting(ctx)
        else:
            prospects = request.existing_prospects
        result.prospects = prospects

        if not prospects:
            logger.warning("Sales pod [%s]: no prospects found — stopping pipeline", job_id)
            result.summary = NO_PROSPECTS_SUMMARY
            return result

        # Stage 2 — Outreach
        dossier_map: dict[str, ProspectDossier] = {}
        if self._should_run(PipelineStage.OUTREACH, ctx.entry):
            result.outreach_sequences, dossier_map = self._run_outreach(ctx, prospects)

        # Stage 3 — Qualification + advance/nurture routing
        qualified: List[QualificationScore] = []
        if self._should_run(PipelineStage.QUALIFICATION, ctx.entry):
            qualified = self._run_qualification(ctx, prospects)
            result.qualified_leads = qualified

        nurture_prospects, qualified_prospects = partition_qualified(qualified, prospects)

        # Stage 4 — Nurturing
        if self._should_run(PipelineStage.NURTURING, ctx.entry) and nurture_prospects:
            result.nurture_sequences = self._run_nurture(ctx, nurture_prospects)

        # Stage 5 — Discovery
        if self._should_run(PipelineStage.DISCOVERY, ctx.entry) and qualified_prospects:
            result.discovery_plans = self._run_discovery(
                ctx, qualified_prospects, qualified, dossier_map
            )

        # Stage 6 — Proposal
        if self._should_run(PipelineStage.PROPOSAL, ctx.entry) and qualified_prospects:
            result.proposals = self._run_proposal(ctx, qualified_prospects, qualified, dossier_map)

        # Stage 7 — Negotiation / Closing
        if self._should_run(PipelineStage.NEGOTIATION, ctx.entry) and qualified_prospects:
            result.closing_strategies = self._run_negotiation(
                ctx,
                qualified_prospects,
                result.proposals,
            )

        # Coaching + outcomes
        result.coaching_report = self._run_coaching(ctx, prospects)
        self._record_prospecting_outcomes(prospects, job_id)

        result.summary = build_pipeline_summary(
            ctx.entry.value,
            prospects=len(result.prospects),
            outreach_sequences=len(result.outreach_sequences),
            qualified_leads=len(result.qualified_leads),
            nurture_sequences=len(result.nurture_sequences),
            discovery_plans=len(result.discovery_plans),
            proposals=len(result.proposals),
            closing_strategies=len(result.closing_strategies),
            insights_version=(current_insights.insights_version if current_insights else None),
            insights_total_outcomes=(
                current_insights.total_outcomes_analyzed if current_insights else 0
            ),
        )

        ctx.update("completed", 100)
        logger.info("Sales pod [%s]: pipeline complete — %s", job_id, result.summary)
        return result

    def _record_prospecting_outcomes(self, prospects: List[Prospect], job_id: str) -> None:
        """Auto-record prospecting outcomes — delegates to the module function.

        Postconditions: same as :func:`record_prospecting_outcomes` (idempotent
        per prospect, never raises).
        """
        record_prospecting_outcomes(prospects, job_id)

    # ------------------------------------------------------------------
    # Convenience single-stage methods (used by standalone API endpoints)
    # ------------------------------------------------------------------

    def _load_insights_ctx(self) -> Optional[str]:
        """Load current insights and format for prompt injection."""
        return format_insights_for_prompt(load_current_insights())

    def prospect_only(
        self,
        icp: IdealCustomerProfile,
        product_name: str,
        value_proposition: str,
        max_prospects: int,
        company_context: str,
    ) -> List[Prospect]:
        ctx = self._load_insights_ctx()
        try:
            result = self.prospector.prospect(
                icp.model_dump_json(indent=2),
                product_name,
                value_proposition,
                max_prospects,
                company_context,
                ctx,
            )
        except Exception:
            logger.exception("sales.prospect_only.failed")
            return []
        return list(result.prospects)

    def outreach_only(
        self,
        prospects: List[Prospect],
        dossier_map: dict[str, ProspectDossier],
        product_name: str,
        value_proposition: str,
        case_study_snippets: List[str],
        company_context: str,
    ) -> List[OutreachSequence]:
        """Generate outreach sequences for a set of prospects.

        Every prospect must have a dossier in ``dossier_map`` keyed by
        ``prospect.id``. Prospects without a dossier are skipped with a
        ``sales.outreach.dossier_missing`` log line.
        """
        ctx = self._load_insights_ctx()
        cases = "\n".join(case_study_snippets)
        sequences: List[OutreachSequence] = []
        for p in prospects:
            dossier = dossier_map.get(p.id)
            if dossier is None:
                logger.warning(
                    "sales.outreach.dossier_missing prospect_id=%s company=%s",
                    p.id,
                    p.company_name,
                )
                continue
            try:
                # outreach_only callers don't supply ICP — pass None and the
                # critic-gated helper falls back to the unreviewed wrap path.
                sequence = self._generate_outreach_with_critic(
                    p,
                    dossier,
                    product_name,
                    value_proposition,
                    cases,
                    company_context,
                    ctx,
                    None,
                    max_refinements=self.config.critic_max_refinements,
                )
            except Exception:
                logger.exception(
                    "sales.outreach_only.failed prospect_id=%s company=%s",
                    p.id,
                    p.company_name,
                )
                continue
            sequences.append(sequence)
        return sequences

    def qualify_only(
        self, prospect: Prospect, product_name: str, value_proposition: str, call_notes: str
    ) -> Optional[QualificationScore]:
        ctx = self._load_insights_ctx()
        try:
            body = self.qualifier.qualify(
                prospect.model_dump_json(indent=2),
                product_name,
                value_proposition,
                call_notes,
                ctx,
            )
        except Exception:
            logger.exception("sales.qualify_only.failed prospect_id=%s", prospect.id)
            return None
        return QualificationScore(prospect=prospect, **body.model_dump())

    def nurture_only(
        self,
        prospects: List[Prospect],
        product_name: str,
        value_proposition: str,
        duration_days: int,
    ) -> List[NurtureSequence]:
        ctx = self._load_insights_ctx()
        sequences: List[NurtureSequence] = []
        for p in prospects:
            try:
                body = self.nurture.build_sequence(
                    p.model_dump_json(indent=2),
                    product_name,
                    value_proposition,
                    duration_days,
                    ctx,
                )
            except Exception:
                logger.exception("sales.nurture_only.failed prospect_id=%s", p.id)
                continue
            sequences.append(NurtureSequence(prospect=p, **body.model_dump()))
        return sequences

    def propose_only(self, req: ProposalRequest) -> Optional[SalesProposal]:
        ctx = self._load_insights_ctx()
        cases = "\n".join(req.case_study_snippets)
        # Best-effort dossier lookup so the proposal critic can score the
        # founded-claims rule. Missing dossier degrades to None — the critic
        # treats that as "(no dossier supplied)" and skips the related rule.
        dossier_map = self.load_dossiers_for_prospects([req.prospect])
        try:
            return self._generate_proposal_with_critic(
                req.prospect,
                req.product_name,
                req.value_proposition,
                req.annual_cost_usd,
                req.discovery_notes,
                cases,
                req.company_context,
                ctx,
                dossier_map.get(req.prospect.id),
                None,  # propose_only does not carry a qualification score
                max_refinements=self.config.critic_max_refinements,
            )
        except Exception:
            logger.exception("sales.propose_only.failed prospect_id=%s", req.prospect.id)
            return None

    def coach_only(
        self, prospects: List[Prospect], product_name: str, pipeline_context: str
    ) -> Optional[PipelineCoachingReport]:
        ctx = self._load_insights_ctx()
        prospects_json = json.dumps([p.model_dump() for p in prospects], indent=2)
        try:
            return self.coach.review(prospects_json, product_name, pipeline_context, ctx)
        except Exception:
            logger.exception("sales.coach_only.failed")
            return None

    # ------------------------------------------------------------------
    # Deep-research prospecting: top-N list + per-prospect dossiers
    # ------------------------------------------------------------------

    def map_company_one(
        self,
        company: Prospect,
        icp_json: str,
        product_name: str,
        value_proposition: str,
        max_per_company: int,
        insights_ctx: Optional[str],
    ) -> List[tuple[Prospect, float]]:
        """Map one company's decision-makers into ``(prospect, confidence)`` pairs.

        The per-company unit of the deep-research map stage — shared by the
        thread-pool fan-out (``deep_research_only``) and the
        ``deep_research_map_company_one`` Temporal activity. Raises on failure;
        callers decide whether to skip (thread mode returns ``[]``; the activity
        re-raises so Temporal can retry).

        Postconditions:
            - Returns the company's decision-maker entries (may be empty if the
              agent found none).
        """
        dm_list = self.decision_maker_mapper.map_contacts(
            company.model_dump_json(indent=2),
            icp_json,
            product_name,
            value_proposition,
            max_per_company,
            insights_ctx,
        )
        return _decision_makers_to_entries(dm_list, company)

    def build_dossier_one(
        self,
        prospect: Prospect,
        product_name: str,
        value_proposition: str,
        insights_ctx: Optional[str],
    ) -> ProspectDossier:
        """Build one prospect's dossier (fully-formed, with ids). Raises on failure.

        The per-prospect unit of the deep-research dossier stage — shared by
        ``deep_research_only`` and the ``deep_research_build_dossier_one``
        activity.

        Preconditions:
            - ``prospect`` has a non-empty ``id`` (assigned by
              :func:`rank_and_assign_ids` before this stage).
        Postconditions:
            - Returns a ``ProspectDossier`` tied to ``prospect`` with
              ``prospect_id``, a ``dsr_<uuid12>`` ``dossier_id``, a
              ``generated_at`` timestamp, and name/title/company/linkedin
              back-filled from the prospect where the agent left them blank.
        """
        dossier = self.dossier_builder.build(
            prospect.model_dump_json(indent=2),
            product_name,
            value_proposition,
            insights_ctx,
        )
        dossier.prospect_id = prospect.id
        if not dossier.full_name and prospect.contact_name:
            dossier.full_name = prospect.contact_name
        if not dossier.current_title and prospect.contact_title:
            dossier.current_title = prospect.contact_title
        if not dossier.current_company:
            dossier.current_company = prospect.company_name
        if not dossier.linkedin_url and prospect.linkedin_url:
            dossier.linkedin_url = prospect.linkedin_url
        if not dossier.dossier_id:
            dossier.dossier_id = f"dsr_{uuid4().hex[:12]}"
        if not dossier.generated_at:
            dossier.generated_at = datetime.now(tz=timezone.utc).isoformat()
        return dossier

    def deep_research_only(
        self,
        request: DeepResearchRequest,
        persist: bool = True,
        dossier_url_builder: Optional[Callable[[str], str]] = None,
    ) -> DeepResearchResult:
        """Run company → decision-maker → dossier and return a ranked top-N list.

        Produces a :class:`DeepResearchResult` where every entry carries a
        stable ``dossier_id`` and ``dossier_url``. If ``persist`` is True
        (default), dossiers and the list are saved via :class:`DossierStore`.
        If the store is unavailable (e.g. ``POSTGRES_HOST`` not set), the
        run still returns a valid result in-memory — the shortfall is noted.

        ``dossier_url_builder`` is an optional callable that maps a
        ``dossier_id`` to the public URL at which that dossier can be
        fetched. Pass ``lambda d: str(request.url_for("get_dossier",
        dossier_id=d))`` from a FastAPI route to produce a URL that matches
        the actual registered path (including any mount prefix). If omitted,
        the URL defaults to ``/api/sales/dossiers/<id>`` which matches the
        unified-api mount; this is a reasonable fallback but not guaranteed
        to match every deployment.
        """
        ctx = self._load_insights_ctx()
        icp_json = request.icp.model_dump_json(indent=2)
        # Request more companies than needed so that dedupe, failures, and
        # the per-company cap leave enough prospects to hit the target.
        companies_requested = min(100, max(40, request.target_prospects))

        # Stage 1 — company shortlist
        try:
            companies_result = self.prospector.prospect_companies(
                icp_json,
                request.product_name,
                request.value_proposition,
                companies_requested,
                request.company_context,
                ctx,
            )
            companies = list(companies_result.prospects)
        except Exception:
            logger.exception("sales.deep_research.company_stage_failed")
            companies = []
        if not companies:
            return assemble_and_persist_deep_research(
                product_name=request.product_name,
                final_prospects=[],
                dossiers={},
                extra_notes=["No companies returned by the prospector agent."],
                target_prospects=request.target_prospects,
                dossier_url_builder=dossier_url_builder,
                persist=persist,
            )

        # Stage 2 — map decision-makers per company (bounded concurrency).
        # parallel_map copies this thread's context per task so the LLM
        # attribution/request-id contextvars propagate into the workers (raw
        # threads don't; see llm_service.attribution). Order is preserved;
        # _map_one returns a list (never None) so skip_none is off.
        def _map_one(company: Prospect) -> List[tuple[Prospect, float]]:
            try:
                return self.map_company_one(
                    company,
                    icp_json,
                    request.product_name,
                    request.value_proposition,
                    request.max_per_company,
                    ctx,
                )
            except Exception:
                logger.exception(
                    "decision-maker mapping failed for company %s", company.company_name
                )
                return []

        mapped: List[tuple[Prospect, float]] = []
        for entries in parallel_map(
            companies,
            _map_one,
            max_workers=self.config.decision_maker_workers,
            skip_none=False,
        ):
            mapped.extend(entries)

        if not mapped:
            return assemble_and_persist_deep_research(
                product_name=request.product_name,
                final_prospects=[],
                dossiers={},
                extra_notes=["No decision-makers identified across the company shortlist."],
                target_prospects=request.target_prospects,
                dossier_url_builder=dossier_url_builder,
                persist=persist,
            )

        # Stage 3 — enforce cap, rank, trim, and assign stable prospect ids
        final_prospects = rank_and_assign_ids(
            mapped, request.max_per_company, request.target_prospects
        )

        # Stage 4 — build dossiers (bounded concurrency; network-heavy).
        # parallel_map copies this thread's context per task (see above);
        # completion order is fine — results feed a dict keyed by prospect id.
        def _build_one(p: Prospect) -> tuple[Prospect, Optional[ProspectDossier]]:
            try:
                return p, self.build_dossier_one(
                    p, request.product_name, request.value_proposition, ctx
                )
            except Exception:
                logger.exception("dossier building failed for prospect %s", p.id)
                return p, None

        dossiers: dict[str, ProspectDossier] = {}
        for p, dossier in parallel_map(
            final_prospects,
            _build_one,
            max_workers=self.config.dossier_workers,
            preserve_order=False,
            skip_none=False,
        ):
            if dossier is not None:
                dossiers[p.id] = dossier

        # Stage 5 — assemble + persist (best-effort)
        return assemble_and_persist_deep_research(
            product_name=request.product_name,
            final_prospects=final_prospects,
            dossiers=dossiers,
            extra_notes=[],
            target_prospects=request.target_prospects,
            dossier_url_builder=dossier_url_builder,
            persist=persist,
        )
