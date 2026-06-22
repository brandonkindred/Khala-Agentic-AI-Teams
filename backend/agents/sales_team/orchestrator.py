"""SalesPodOrchestrator — coordinates all sales team agents through the pipeline."""

from __future__ import annotations

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional
from uuid import uuid4

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

logger = logging.getLogger(__name__)

UpdateCallback = Callable[[str, int], None]

_STAGE_ORDER = [
    PipelineStage.PROSPECTING,
    PipelineStage.OUTREACH,
    PipelineStage.QUALIFICATION,
    PipelineStage.NURTURING,
    PipelineStage.DISCOVERY,
    PipelineStage.PROPOSAL,
    PipelineStage.NEGOTIATION,
]


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
        self.prospector = ProspectorAgent()
        self.outreach = OutreachAgent()
        self.qualifier = LeadQualifierAgent()
        self.nurture = NurtureAgent()
        self.discovery = DiscoveryAgent()
        self.proposal = ProposalAgent()
        self.closer = CloserAgent()
        self.coach = SalesCoachAgent()
        self.decision_maker_mapper = DecisionMakerMapperAgent()
        self.dossier_builder = DossierBuilderAgent()
        self.learning_engine = LearningEngine()
        self.outreach_critic = OutreachCriticAgent()
        self.proposal_critic = ProposalCriticAgent()

    def _should_run(self, stage: PipelineStage, entry: PipelineStage) -> bool:
        try:
            return _STAGE_ORDER.index(stage) >= _STAGE_ORDER.index(entry)
        except ValueError:
            return False

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
        """Emit -> wrap -> critic -> on revise, re-emit up to *max_refinements* times."""
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

        Returns a map keyed by prospect.id. Prospects without a saved dossier
        are simply absent from the map — the caller decides whether to skip
        them. Safe to call when Postgres is unreachable (returns empty map).

        Public so HTTP handlers can build the ``dossier_map`` argument for
        :meth:`outreach_only` by prospect id.
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

    # ------------------------------------------------------------------
    # Per-stage methods — each owns a single pipeline phase
    # ------------------------------------------------------------------

    def _run_prospecting(self, ctx: _RunContext) -> List[Prospect]:
        ctx.update("prospecting", 5)
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
        ctx.update("prospecting", 15)
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
        if not prospects:
            return []
        results: list = [None] * len(prospects)
        workers = min(self.config.pipeline_stage_workers, len(prospects))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # A fresh context copy per task: a single Context can't be entered
            # concurrently, and each worker must see the parent's attribution.
            idx_by_future = {
                pool.submit(contextvars.copy_context().run, fn, p): i
                for i, p in enumerate(prospects)
            }
            for fut in as_completed(idx_by_future):
                results[idx_by_future[fut]] = fut.result()
        return [r for r in results if r is not None]

    def _run_outreach(
        self,
        ctx: _RunContext,
        prospects: List[Prospect],
    ) -> tuple[List[OutreachSequence], dict[str, ProspectDossier]]:
        ctx.update("outreach", 20)
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
            except Exception:
                logger.exception(
                    "sales.outreach.failed prospect_id=%s company=%s",
                    p.id,
                    p.company_name,
                )
                return None

        sequences = self._map_prospects_parallel(prospects, _one)
        ctx.update("outreach", 35)
        return sequences, dossier_map

    def _run_qualification(
        self,
        ctx: _RunContext,
        prospects: List[Prospect],
    ) -> List[QualificationScore]:
        ctx.update("qualification", 40)
        logger.info("Sales pod [%s]: qualification stage", ctx.job_id)

        def _one(p: Prospect) -> Optional[QualificationScore]:
            try:
                body = self.qualifier.qualify(
                    p.model_dump_json(indent=2),
                    ctx.product,
                    ctx.vp,
                    "",
                    ctx.insights_ctx,
                )
                return QualificationScore(prospect=p, **body.model_dump())
            except Exception:
                logger.exception("sales.qualify.failed prospect_id=%s", p.id)
                return None

        qualified = self._map_prospects_parallel(prospects, _one)
        ctx.update("qualification", 50)
        return qualified

    def _run_nurture(
        self,
        ctx: _RunContext,
        nurture_prospects: List[Prospect],
    ) -> List[NurtureSequence]:
        ctx.update("nurturing", 55)
        logger.info("Sales pod [%s]: nurturing %d prospects", ctx.job_id, len(nurture_prospects))

        def _one(p: Prospect) -> Optional[NurtureSequence]:
            try:
                body = self.nurture.build_sequence(
                    p.model_dump_json(indent=2),
                    ctx.product,
                    ctx.vp,
                    90,
                    ctx.insights_ctx,
                )
                return NurtureSequence(prospect=p, **body.model_dump())
            except Exception:
                logger.exception("sales.nurture.failed prospect_id=%s", p.id)
                return None

        nurture_seqs = self._map_prospects_parallel(nurture_prospects, _one)
        ctx.update("nurturing", 62)
        return nurture_seqs

    def _run_discovery(
        self,
        ctx: _RunContext,
        qualified_prospects: List[Prospect],
        qualified: List[QualificationScore],
    ) -> List[DiscoveryPlan]:
        ctx.update("discovery", 65)
        logger.info(
            "Sales pod [%s]: discovery stage for %d prospects",
            ctx.job_id,
            len(qualified_prospects),
        )

        def _one(p: Prospect) -> Optional[DiscoveryPlan]:
            qual_json = "{}"
            for q in qualified:
                if q.prospect.company_name == p.company_name:
                    qual_json = q.model_dump_json(indent=2)
                    break
            try:
                body = self.discovery.prepare(
                    p.model_dump_json(indent=2),
                    qual_json,
                    ctx.product,
                    ctx.vp,
                    ctx.insights_ctx,
                )
                return DiscoveryPlan(prospect=p, **body.model_dump())
            except Exception:
                logger.exception("sales.discovery.failed prospect_id=%s", p.id)
                return None

        plans = self._map_prospects_parallel(qualified_prospects, _one)
        ctx.update("discovery", 75)
        return plans

    def _run_proposal(
        self,
        ctx: _RunContext,
        qualified_prospects: List[Prospect],
        qualified: List[QualificationScore],
        dossier_map: dict[str, ProspectDossier],
    ) -> List[SalesProposal]:
        ctx.update("proposal", 78)
        logger.info(
            "Sales pod [%s]: proposal stage for %d prospects",
            ctx.job_id,
            len(qualified_prospects),
        )
        annual_cost = 25000.0
        qual_by_prospect_id = {q.prospect.id: q for q in qualified if q.prospect.id}
        if not dossier_map:
            dossier_map = self.load_dossiers_for_prospects(qualified_prospects)

        def _one(p: Prospect) -> Optional[SalesProposal]:
            try:
                return self._generate_proposal_with_critic(
                    p,
                    ctx.product,
                    ctx.vp,
                    annual_cost,
                    "",
                    ctx.cases,
                    ctx.company_context,
                    ctx.insights_ctx,
                    dossier_map.get(p.id),
                    qual_by_prospect_id.get(p.id),
                    max_refinements=ctx.config.critic_max_refinements,
                )
            except Exception:
                logger.exception("sales.proposal.failed prospect_id=%s", p.id)
                return None

        proposals = self._map_prospects_parallel(qualified_prospects, _one)
        ctx.update("proposal", 87)
        return proposals

    def _run_negotiation(
        self,
        ctx: _RunContext,
        qualified_prospects: List[Prospect],
        proposals: List[SalesProposal],
    ) -> List[ClosingStrategy]:
        ctx.update("negotiation", 90)
        logger.info("Sales pod [%s]: closing strategy stage", ctx.job_id)

        def _one(p: Prospect) -> Optional[ClosingStrategy]:
            prop_json = "{}"
            for prop in proposals:
                if prop.prospect.company_name == p.company_name:
                    prop_json = prop.model_dump_json(indent=2)
                    break
            try:
                body = self.closer.develop_strategy(
                    p.model_dump_json(indent=2),
                    prop_json,
                    ctx.product,
                    ctx.vp,
                    ctx.insights_ctx,
                )
                return ClosingStrategy(prospect=p, **body.model_dump())
            except Exception:
                logger.exception("sales.close.failed prospect_id=%s", p.id)
                return None

        strategies = self._map_prospects_parallel(qualified_prospects, _one)
        ctx.update("negotiation", 95)
        return strategies

    def _run_coaching(
        self,
        ctx: _RunContext,
        prospects: List[Prospect],
    ) -> Optional[PipelineCoachingReport]:
        ctx.update("coaching", 97)
        logger.info("Sales pod [%s]: generating coaching report", ctx.job_id)
        prospects_json = json.dumps([p.model_dump() for p in prospects], indent=2)
        try:
            return self.coach.review(prospects_json, ctx.product, "", ctx.insights_ctx)
        except Exception:
            logger.exception("sales.coaching.failed")
            return None

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

        ctx = _RunContext(
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
            update=update_cb or _noop_update,
        )
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
            result.summary = "No prospects found or provided. Pipeline halted."
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

        if qualified:
            advance = [q for q in qualified if q.recommended_action.lower().startswith("advance")]
            nurture_prospects = [
                q.prospect
                for q in qualified
                if not q.recommended_action.lower().startswith("advance")
                and not q.recommended_action.lower().startswith("disqualify")
            ]
            qualified_prospects = [q.prospect for q in advance]
        else:
            nurture_prospects = []
            qualified_prospects = prospects

        # Stage 4 — Nurturing
        if self._should_run(PipelineStage.NURTURING, ctx.entry) and nurture_prospects:
            result.nurture_sequences = self._run_nurture(ctx, nurture_prospects)

        # Stage 5 — Discovery
        if self._should_run(PipelineStage.DISCOVERY, ctx.entry) and qualified_prospects:
            result.discovery_plans = self._run_discovery(ctx, qualified_prospects, qualified)

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

        insights_note = (
            f" (learning insights v{current_insights.insights_version} applied)"
            if current_insights and current_insights.total_outcomes_analyzed > 0
            else " (no learning history yet — record outcomes to improve future runs)"
        )
        result.summary = (
            f"Sales pod completed pipeline from '{ctx.entry.value}' stage{insights_note}. "
            f"Prospects identified: {len(result.prospects)}. "
            f"Outreach sequences generated: {len(result.outreach_sequences)}. "
            f"Leads qualified: {len(result.qualified_leads)}. "
            f"Nurture sequences: {len(result.nurture_sequences)}. "
            f"Discovery plans: {len(result.discovery_plans)}. "
            f"Proposals written: {len(result.proposals)}. "
            f"Closing strategies: {len(result.closing_strategies)}."
        )

        ctx.update("completed", 100)
        logger.info("Sales pod [%s]: pipeline complete — %s", job_id, result.summary)
        return result

    def _record_prospecting_outcomes(self, prospects: List[Prospect], job_id: str) -> None:
        """Auto-record each identified prospect as a PROSPECTING / CONVERTED stage outcome.

        This seeds the outcome store so the learning engine has data even before
        the user manually records deal-level outcomes.
        """
        for p in prospects:
            try:
                record_stage_outcome(
                    StageOutcome(
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
        if dossier_url_builder is None:

            def dossier_url_builder(dossier_id: str) -> str:
                return f"/api/sales/dossiers/{dossier_id}"

        ctx = self._load_insights_ctx()
        icp_json = request.icp.model_dump_json(indent=2)
        # Request more companies than needed so that dedupe, failures, and
        # the per-company cap leave enough prospects to hit the target.
        companies_requested = min(100, max(40, request.target_prospects))
        run_notes: List[str] = []

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
            run_notes.append("No companies returned by the prospector agent.")
            return DeepResearchResult(
                list_id="",
                product_name=request.product_name,
                generated_at=datetime.now(tz=timezone.utc).isoformat(),
                total_prospects=0,
                companies_represented=0,
                entries=[],
                notes="; ".join(run_notes),
            )

        # Stage 2 — map decision-makers per company (bounded concurrency)
        mapped: List[tuple[Prospect, float]] = []

        def _map_one(company: Prospect) -> List[tuple[Prospect, float]]:
            try:
                dm_list = self.decision_maker_mapper.map_contacts(
                    company.model_dump_json(indent=2),
                    icp_json,
                    request.product_name,
                    request.value_proposition,
                    request.max_per_company,
                    ctx,
                )
                return _decision_makers_to_entries(dm_list, company)
            except Exception:
                logger.exception(
                    "decision-maker mapping failed for company %s", company.company_name
                )
                return []

        # submit (not pool.map) so each call runs inside a fresh copy of this
        # thread's context — the LLM attribution/request-id contextvars do not
        # propagate to raw worker threads (see llm_service.attribution). Iterating
        # the futures in submission order preserves pool.map's input ordering.
        with ThreadPoolExecutor(max_workers=self.config.decision_maker_workers) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _map_one, company)
                for company in companies
            ]
            for fut in futures:
                mapped.extend(fut.result())

        if not mapped:
            run_notes.append("No decision-makers identified across the company shortlist.")
            return DeepResearchResult(
                list_id="",
                product_name=request.product_name,
                generated_at=datetime.now(tz=timezone.utc).isoformat(),
                total_prospects=0,
                companies_represented=0,
                entries=[],
                notes="; ".join(run_notes),
            )

        # Stage 3 — enforce ≤max_per_company, rank, trim to target
        final_prospects = _enforce_cap_and_rank(
            mapped, request.max_per_company, request.target_prospects
        )
        if len(final_prospects) < request.target_prospects:
            run_notes.append(
                f"Only {len(final_prospects)} qualifying prospects after per-company cap "
                f"(target was {request.target_prospects})."
            )

        # Assign stable prospect IDs before dossier building so dossiers can
        # reference them.
        for p in final_prospects:
            if not p.id:
                p.id = f"prs_{uuid4().hex[:12]}"

        # Stage 4 — build dossiers (bounded concurrency; network-heavy)
        def _build_one(p: Prospect) -> tuple[Prospect, Optional[ProspectDossier]]:
            try:
                dossier = self.dossier_builder.build(
                    p.model_dump_json(indent=2),
                    request.product_name,
                    request.value_proposition,
                    ctx,
                )
                # Ensure the dossier is tied to the prospect we asked about —
                # the model is instructed to set prospect_id but we enforce it
                # here so later persistence + lookups always work.
                dossier.prospect_id = p.id
                if not dossier.full_name and p.contact_name:
                    dossier.full_name = p.contact_name
                if not dossier.current_title and p.contact_title:
                    dossier.current_title = p.contact_title
                if not dossier.current_company:
                    dossier.current_company = p.company_name
                if not dossier.linkedin_url and p.linkedin_url:
                    dossier.linkedin_url = p.linkedin_url
                return p, dossier
            except Exception:
                logger.exception("dossier building failed for prospect %s", p.id)
                return p, None

        dossiers: dict[str, ProspectDossier] = {}
        # copy_context().run per task so attribution propagates into the workers
        # (raw ThreadPoolExecutor does not copy contextvars; see llm_service.attribution).
        with ThreadPoolExecutor(max_workers=self.config.dossier_workers) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _build_one, p) for p in final_prospects
            ]
            for fut in as_completed(futures):
                p, dossier = fut.result()
                if dossier is None:
                    continue
                # Ensure dossier has IDs before potential persistence.
                if not dossier.dossier_id:
                    dossier.dossier_id = f"dsr_{uuid4().hex[:12]}"
                if not dossier.generated_at:
                    dossier.generated_at = datetime.now(tz=timezone.utc).isoformat()
                dossier.prospect_id = p.id
                dossiers[p.id] = dossier

        # Stage 5 — persist (best-effort) and assemble the result
        store = None
        if persist:
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
                    dossier_url=dossier_url_builder(dossier.dossier_id),
                )
            )

        result = DeepResearchResult(
            list_id="",
            product_name=request.product_name,
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
            total_prospects=len(entries),
            companies_represented=len({e.prospect.company_name for e in entries}),
            entries=entries,
            notes="; ".join(run_notes),
        )
        if store is not None:
            try:
                result = store.save_prospect_list(result)
            except Exception:
                logger.exception("Failed to persist prospect list")
        return result
