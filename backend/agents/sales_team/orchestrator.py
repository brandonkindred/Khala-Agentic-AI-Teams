"""SalesPodOrchestrator — coordinates all sales team agents through the pipeline."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from .learning_engine import LearningEngine, format_insights_for_prompt
from .models import (
    BANTScore,
    ClosingStrategy,
    DeepResearchRequest,
    DeepResearchResult,
    DiscoveryPlan,
    IdealCustomerProfile,
    LearningInsights,
    MEDDICScore,
    NurtureSequence,
    ObjectionHandler,
    OutcomeResult,
    OutreachSequence,
    PipelineCoachingReport,
    PipelineStage,
    ProposalRequest,
    Prospect,
    ProspectDossier,
    ProspectListEntry,
    QualificationScore,
    ROIModel,
    SalesPipelineRequest,
    SalesPipelineResult,
    SalesProposal,
    SPINQuestions,
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


def _parse_json(raw: str, fallback: object) -> object:
    """Best-effort JSON parse; returns fallback on failure."""
    if not raw or not raw.strip():
        return fallback
    # Strip markdown code fences if the LLM wrapped the output
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Could not parse agent JSON output; using fallback. Raw: %s", raw[:200])
        return fallback


def _prospects_from_json(raw: str) -> List[Prospect]:
    data = _parse_json(raw, [])
    if not isinstance(data, list):
        data = [data] if isinstance(data, dict) else []
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            results.append(Prospect(**item))
        except Exception as exc:
            logger.warning("Could not parse prospect: %s — %s", item, exc)
    return results


def _decision_makers_from_json(raw: str, company: Prospect) -> List[tuple[Prospect, float]]:
    """Parse a decision-maker mapper JSON array into ``(prospect, confidence)`` tuples.

    Each returned Prospect inherits company-level data (company_name, website,
    industry, icp_match_score, etc.) from ``company`` and overlays the contact
    fields (contact_name, contact_title, linkedin_url). Confidence is a 0–1
    score from the mapper agent used later to break ties during ranking.
    """
    data = _parse_json(raw, [])
    if not isinstance(data, list):
        data = [data] if isinstance(data, dict) else []
    results: List[tuple[Prospect, float]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("contact_name")
        if not name:
            continue
        rationale = item.get("decision_maker_rationale") or ""
        confidence_raw = item.get("confidence")
        extra_notes = rationale
        if confidence_raw is not None:
            extra_notes = f"{rationale} (confidence: {confidence_raw})".strip()
        notes = company.research_notes
        combined_notes = (notes + "\n" + extra_notes).strip() if extra_notes else notes
        try:
            prospect = Prospect(
                company_name=company.company_name,
                website=company.website,
                contact_name=name,
                contact_title=item.get("contact_title"),
                contact_email=None,  # never fabricate emails
                linkedin_url=item.get("linkedin_url"),
                company_size_estimate=company.company_size_estimate,
                industry=company.industry,
                icp_match_score=company.icp_match_score,
                research_notes=combined_notes,
                trigger_events=list(company.trigger_events or []),
            )
            confidence = float(confidence_raw) if confidence_raw is not None else 0.5
            results.append((prospect, confidence))
        except Exception as exc:
            logger.warning("Could not parse decision-maker contact: %s — %s", item, exc)
    return results


def _dossier_from_json(raw: str, prospect: Prospect) -> Optional[ProspectDossier]:
    """Parse a dossier-builder JSON object into a ProspectDossier."""
    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    # Ensure we always tie the dossier back to the prospect we asked about.
    data.setdefault("prospect_id", prospect.id)
    data.setdefault("full_name", prospect.contact_name or "")
    data.setdefault("current_title", prospect.contact_title or "")
    data.setdefault("current_company", prospect.company_name)
    # Inherit linkedin if the agent didn't return one.
    if not data.get("linkedin_url"):
        data["linkedin_url"] = prospect.linkedin_url
    try:
        return ProspectDossier.model_validate(data)
    except Exception as exc:
        logger.warning(
            "Could not parse dossier for prospect %s (%s): %s",
            prospect.id,
            prospect.contact_name,
            exc,
        )
        return None


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

    ``entries`` is a list of ``(prospect, confidence)`` pairs produced by
    :func:`_decision_makers_from_json`. Returns a plain ``List[Prospect]``
    ordered by rank score descending.

    Rules:
    1. Drop duplicates by (company_name, linkedin_url or contact_name).
    2. For each company, keep only the top ``max_per_company`` contacts by
       their rank score.
    3. Sort the surviving list globally by rank score desc and trim to
       ``target_count``.
    """
    # Step 1: dedupe within the input
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

    # Step 2: per-company cap (keep top ``max_per_company`` per company by rank score)
    by_company: dict[str, List[tuple[Prospect, float]]] = {}
    for entry in deduped:
        p = entry[0]
        by_company.setdefault((p.company_name or "").strip().lower(), []).append(entry)

    capped: List[tuple[Prospect, float]] = []
    for company_list in by_company.values():
        company_list.sort(key=_rank_score, reverse=True)
        capped.extend(company_list[:max_per_company])

    # Step 3: global rank + trim
    capped.sort(key=_rank_score, reverse=True)
    return [entry[0] for entry in capped[:target_count]]


def _qual_from_json(raw: str, prospect: Prospect) -> Optional[QualificationScore]:
    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        bant_data = data.get("bant", {})
        meddic_data = data.get("meddic", {})
        return QualificationScore(
            prospect=prospect,
            bant=BANTScore(**bant_data),
            meddic=MEDDICScore(**meddic_data),
            overall_score=float(data.get("overall_score", 0.5)),
            value_creation_level=int(data.get("value_creation_level", 2)),
            recommended_action=data.get("recommended_action", "Nurture"),
            disqualification_reason=data.get("disqualification_reason"),
            qualification_notes=data.get("qualification_notes", ""),
        )
    except Exception as exc:
        logger.warning("Could not build QualificationScore: %s", exc)
        return None


def _outreach_from_json(raw: str, prospect: Prospect) -> Optional[OutreachSequence]:
    from .models import EmailTouch

    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        email_seq = [EmailTouch(**e) for e in data.get("email_sequence", [])]
        return OutreachSequence(
            prospect=prospect,
            email_sequence=email_seq,
            call_script=data.get("call_script", ""),
            linkedin_message=data.get("linkedin_message", ""),
            sequence_rationale=data.get("sequence_rationale", ""),
        )
    except Exception as exc:
        logger.warning("Could not build OutreachSequence: %s", exc)
        return None


def _nurture_from_json(
    raw: str, prospect: Prospect, duration_days: int
) -> Optional[NurtureSequence]:
    from .models import NurtureTouchpoint, OutreachChannel

    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        touchpoints = []
        for t in data.get("touchpoints", []):
            tp = NurtureTouchpoint(
                day=t.get("day", 0),
                channel=OutreachChannel(t.get("channel", "email")),
                content_type=t.get("content_type", "email"),
                message=t.get("message", ""),
                goal=t.get("goal", ""),
            )
            touchpoints.append(tp)
        return NurtureSequence(
            prospect=prospect,
            duration_days=data.get("duration_days", duration_days),
            touchpoints=touchpoints,
            re_engagement_triggers=data.get("re_engagement_triggers", []),
            content_recommendations=data.get("content_recommendations", []),
        )
    except Exception as exc:
        logger.warning("Could not build NurtureSequence: %s", exc)
        return None


def _discovery_from_json(raw: str, prospect: Prospect) -> Optional[DiscoveryPlan]:
    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        sq = data.get("spin_questions", {})
        return DiscoveryPlan(
            prospect=prospect,
            spin_questions=SPINQuestions(
                situation=sq.get("situation", []),
                problem=sq.get("problem", []),
                implication=sq.get("implication", []),
                need_payoff=sq.get("need_payoff", []),
            ),
            challenger_insight=data.get("challenger_insight", ""),
            demo_agenda=data.get("demo_agenda", []),
            expected_objections=data.get("expected_objections", []),
            success_criteria_for_call=data.get("success_criteria_for_call", ""),
        )
    except Exception as exc:
        logger.warning("Could not build DiscoveryPlan: %s", exc)
        return None


def _proposal_from_json(
    raw: str, prospect: Prospect, annual_cost: float
) -> Optional[SalesProposal]:
    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        roi_data = data.get("roi_model", {})
        roi = ROIModel(
            annual_cost_usd=float(roi_data.get("annual_cost_usd", annual_cost)),
            estimated_annual_benefit_usd=float(
                roi_data.get("estimated_annual_benefit_usd", annual_cost * 2.5)
            ),
            payback_months=float(roi_data.get("payback_months", 8.0)),
            roi_percentage=float(roi_data.get("roi_percentage", 150.0)),
            assumptions=roi_data.get("assumptions", []),
        )
        from .models import ProposalSection

        custom_sections = [
            ProposalSection(**s) for s in data.get("custom_sections", []) if isinstance(s, dict)
        ]
        return SalesProposal(
            prospect=prospect,
            executive_summary=data.get("executive_summary", ""),
            situation_analysis=data.get("situation_analysis", ""),
            proposed_solution=data.get("proposed_solution", ""),
            roi_model=roi,
            investment_table=data.get("investment_table", ""),
            implementation_timeline=data.get("implementation_timeline", ""),
            risk_mitigation=data.get("risk_mitigation", ""),
            next_steps=data.get("next_steps", []),
            custom_sections=custom_sections,
        )
    except Exception as exc:
        logger.warning("Could not build SalesProposal: %s", exc)
        return None


def _closing_from_json(raw: str, prospect: Prospect) -> Optional[ClosingStrategy]:
    from .models import CloseType

    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        handlers = [
            ObjectionHandler(
                objection=h.get("objection", ""),
                response=h.get("response", ""),
                feel_felt_found=h.get("feel_felt_found"),
            )
            for h in data.get("objection_handlers", [])
            if isinstance(h, dict)
        ]
        technique_raw = data.get("recommended_close_technique", "summary")
        try:
            technique = CloseType(technique_raw)
        except ValueError:
            technique = CloseType.SUMMARY
        return ClosingStrategy(
            prospect=prospect,
            recommended_close_technique=technique,
            close_script=data.get("close_script", ""),
            objection_handlers=handlers,
            urgency_framing=data.get("urgency_framing", ""),
            walk_away_criteria=data.get("walk_away_criteria", ""),
            emotional_intelligence_notes=data.get("emotional_intelligence_notes", ""),
        )
    except Exception as exc:
        logger.warning("Could not build ClosingStrategy: %s", exc)
        return None


def _coaching_from_json(raw: str, n_prospects: int) -> Optional[PipelineCoachingReport]:
    from .models import DealRiskSignal, ForecastCategory

    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        signals = [
            DealRiskSignal(
                signal=s.get("signal", ""),
                severity=s.get("severity", "medium"),
                recommended_action=s.get("recommended_action", ""),
            )
            for s in data.get("deal_risk_signals", [])
            if isinstance(s, dict)
        ]
        fc_raw = data.get("forecast_category", "pipeline")
        try:
            fc = ForecastCategory(fc_raw)
        except ValueError:
            fc = ForecastCategory.PIPELINE
        return PipelineCoachingReport(
            prospects_reviewed=data.get("prospects_reviewed", n_prospects),
            deal_risk_signals=signals,
            talk_listen_ratio_advice=data.get("talk_listen_ratio_advice", ""),
            velocity_insights=data.get("velocity_insights", ""),
            forecast_category=fc,
            top_priority_deals=data.get("top_priority_deals", []),
            recommended_next_actions=data.get("recommended_next_actions", []),
            coaching_summary=data.get("coaching_summary", ""),
        )
    except Exception as exc:
        logger.warning("Could not build PipelineCoachingReport: %s", exc)
        return None


class SalesPodOrchestrator:
    """Coordinates all sales pod agents through the full pipeline.

    Stages run sequentially from the requested entry point. Each stage's
    output is passed as context to the next stage.

    On each run, current LearningInsights are loaded from the outcome store
    and injected into every agent prompt so the pod continuously improves
    based on historical win/loss data.
    """

    def __init__(self) -> None:
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

    def _should_run(self, stage: PipelineStage, entry: PipelineStage) -> bool:
        try:
            return _STAGE_ORDER.index(stage) >= _STAGE_ORDER.index(entry)
        except ValueError:
            return False

    def run(
        self,
        request: SalesPipelineRequest,
        job_id: str,
        update_cb: Optional[UpdateCallback] = None,
    ) -> SalesPipelineResult:
        def update(stage: str, pct: int) -> None:
            if update_cb:
                update_cb(stage, pct)

        icp_json = request.icp.model_dump_json(indent=2)
        product = request.product_name
        vp = request.value_proposition
        ctx = request.company_context
        cases = "\n".join(request.case_study_snippets) if request.case_study_snippets else ""
        entry = request.entry_stage

        # Load current learning insights and format them for prompt injection
        current_insights: Optional[LearningInsights] = load_current_insights()
        insights_ctx: Optional[str] = format_insights_for_prompt(current_insights)
        if current_insights and current_insights.total_outcomes_analyzed > 0:
            logger.info(
                "Sales pod [%s]: injecting learning insights v%d (%d outcomes, win_rate=%.0f%%)",
                job_id,
                current_insights.insights_version,
                current_insights.total_outcomes_analyzed,
                current_insights.win_rate * 100,
            )

        result = SalesPipelineResult(job_id=job_id, entry_stage=entry, product_name=product)

        # ------------------------------------------------------------------
        # Stage 1: Prospecting
        # ------------------------------------------------------------------
        if self._should_run(PipelineStage.PROSPECTING, entry):
            update("prospecting", 5)
            logger.info("Sales pod [%s]: prospecting stage", job_id)
            if request.existing_prospects:
                prospects = request.existing_prospects
            else:
                raw = self.prospector.prospect(
                    icp_json, product, vp, request.max_prospects, ctx, insights_ctx
                )
                prospects = _prospects_from_json(raw)
            result.prospects = prospects
            update("prospecting", 15)
        else:
            prospects = request.existing_prospects
            result.prospects = prospects

        if not prospects:
            logger.warning("Sales pod [%s]: no prospects found — stopping pipeline", job_id)
            result.summary = "No prospects found or provided. Pipeline halted."
            return result

        # ------------------------------------------------------------------
        # Stage 2: Outreach
        # ------------------------------------------------------------------
        if self._should_run(PipelineStage.OUTREACH, entry):
            update("outreach", 20)
            logger.info("Sales pod [%s]: outreach stage for %d prospects", job_id, len(prospects))
            sequences: List[OutreachSequence] = []
            for p in prospects:
                raw = self.outreach.generate_sequence(
                    p.model_dump_json(indent=2), product, vp, cases, ctx, insights_ctx
                )
                seq = _outreach_from_json(raw, p)
                if seq:
                    sequences.append(seq)
            result.outreach_sequences = sequences
            update("outreach", 35)

        # ------------------------------------------------------------------
        # Stage 3: Qualification
        # ------------------------------------------------------------------
        qualified: List[QualificationScore] = []
        if self._should_run(PipelineStage.QUALIFICATION, entry):
            update("qualification", 40)
            logger.info("Sales pod [%s]: qualification stage", job_id)
            for p in prospects:
                raw = self.qualifier.qualify(
                    p.model_dump_json(indent=2), product, vp, "", insights_ctx
                )
                score = _qual_from_json(raw, p)
                if score:
                    qualified.append(score)
            result.qualified_leads = qualified
            update("qualification", 50)

        # Determine which prospects advance vs. go to nurture
        if qualified:
            # Qualification ran — only explicitly advanced leads proceed downstream.
            # Disqualified and stalled leads are intentionally excluded.
            advance = [q for q in qualified if q.recommended_action.lower().startswith("advance")]
            nurture_prospects = [
                q.prospect
                for q in qualified
                if not q.recommended_action.lower().startswith("advance")
                and not q.recommended_action.lower().startswith("disqualify")
            ]
            qualified_prospects = [q.prospect for q in advance]
        else:
            # No qualification ran — all prospects advance to downstream stages
            advance = []
            nurture_prospects = []
            qualified_prospects = prospects

        # ------------------------------------------------------------------
        # Stage 4: Nurturing
        # ------------------------------------------------------------------
        if self._should_run(PipelineStage.NURTURING, entry) and nurture_prospects:
            update("nurturing", 55)
            logger.info("Sales pod [%s]: nurturing %d prospects", job_id, len(nurture_prospects))
            nurture_seqs: List[NurtureSequence] = []
            for p in nurture_prospects:
                raw = self.nurture.build_sequence(
                    p.model_dump_json(indent=2), product, vp, 90, insights_ctx
                )
                seq = _nurture_from_json(raw, p, 90)
                if seq:
                    nurture_seqs.append(seq)
            result.nurture_sequences = nurture_seqs
            update("nurturing", 62)

        # ------------------------------------------------------------------
        # Stage 5: Discovery
        # ------------------------------------------------------------------
        if self._should_run(PipelineStage.DISCOVERY, entry) and qualified_prospects:
            update("discovery", 65)
            logger.info(
                "Sales pod [%s]: discovery stage for %d prospects", job_id, len(qualified_prospects)
            )
            plans: List[DiscoveryPlan] = []
            for p in qualified_prospects:
                qual_json = "{}"
                for q in qualified:
                    if q.prospect.company_name == p.company_name:
                        qual_json = q.model_dump_json(indent=2)
                        break
                raw = self.discovery.prepare(
                    p.model_dump_json(indent=2), qual_json, product, vp, insights_ctx
                )
                plan = _discovery_from_json(raw, p)
                if plan:
                    plans.append(plan)
            result.discovery_plans = plans
            update("discovery", 75)

        # ------------------------------------------------------------------
        # Stage 6: Proposal
        # ------------------------------------------------------------------
        if self._should_run(PipelineStage.PROPOSAL, entry) and qualified_prospects:
            update("proposal", 78)
            logger.info(
                "Sales pod [%s]: proposal stage for %d prospects", job_id, len(qualified_prospects)
            )
            proposals: List[SalesProposal] = []
            annual_cost = 25000.0  # Default; real requests should supply per-prospect pricing
            for p in qualified_prospects:
                raw = self.proposal.write(
                    p.model_dump_json(indent=2),
                    product,
                    vp,
                    annual_cost,
                    "",
                    cases,
                    ctx,
                    insights_ctx,
                )
                prop = _proposal_from_json(raw, p, annual_cost)
                if prop:
                    proposals.append(prop)
            result.proposals = proposals
            update("proposal", 87)

        # ------------------------------------------------------------------
        # Stage 7: Negotiation / Closing
        # ------------------------------------------------------------------
        if self._should_run(PipelineStage.NEGOTIATION, entry) and qualified_prospects:
            update("negotiation", 90)
            logger.info("Sales pod [%s]: closing strategy stage", job_id)
            strategies: List[ClosingStrategy] = []
            for p in qualified_prospects:
                prop_json = "{}"
                for prop in result.proposals:
                    if prop.prospect.company_name == p.company_name:
                        prop_json = prop.model_dump_json(indent=2)
                        break
                raw = self.closer.develop_strategy(
                    p.model_dump_json(indent=2), prop_json, product, vp, insights_ctx
                )
                strat = _closing_from_json(raw, p)
                if strat:
                    strategies.append(strat)
            result.closing_strategies = strategies
            update("negotiation", 95)

        # ------------------------------------------------------------------
        # Final: Pipeline Coaching Report
        # ------------------------------------------------------------------
        update("coaching", 97)
        logger.info("Sales pod [%s]: generating coaching report", job_id)
        prospects_json = json.dumps([p.model_dump() for p in prospects], indent=2)
        raw = self.coach.review(prospects_json, product, "", insights_ctx)
        coaching = _coaching_from_json(raw, len(prospects))
        result.coaching_report = coaching

        # Auto-record prospecting outcomes so the ICP accuracy learns over time
        self._record_prospecting_outcomes(result.prospects, job_id)

        # Summary
        insights_note = (
            f" (learning insights v{current_insights.insights_version} applied)"
            if current_insights and current_insights.total_outcomes_analyzed > 0
            else " (no learning history yet — record outcomes to improve future runs)"
        )
        result.summary = (
            f"Sales pod completed pipeline from '{entry.value}' stage{insights_note}. "
            f"Prospects identified: {len(result.prospects)}. "
            f"Outreach sequences generated: {len(result.outreach_sequences)}. "
            f"Leads qualified: {len(result.qualified_leads)}. "
            f"Nurture sequences: {len(result.nurture_sequences)}. "
            f"Discovery plans: {len(result.discovery_plans)}. "
            f"Proposals written: {len(result.proposals)}. "
            f"Closing strategies: {len(result.closing_strategies)}."
        )

        update("completed", 100)
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
        raw = self.prospector.prospect(
            icp.model_dump_json(indent=2),
            product_name,
            value_proposition,
            max_prospects,
            company_context,
            ctx,
        )
        return _prospects_from_json(raw)

    def outreach_only(
        self,
        prospects: List[Prospect],
        product_name: str,
        value_proposition: str,
        case_study_snippets: List[str],
        company_context: str,
    ) -> List[OutreachSequence]:
        ctx = self._load_insights_ctx()
        cases = "\n".join(case_study_snippets)
        sequences = []
        for p in prospects:
            raw = self.outreach.generate_sequence(
                p.model_dump_json(indent=2),
                product_name,
                value_proposition,
                cases,
                company_context,
                ctx,
            )
            seq = _outreach_from_json(raw, p)
            if seq:
                sequences.append(seq)
        return sequences

    def qualify_only(
        self, prospect: Prospect, product_name: str, value_proposition: str, call_notes: str
    ) -> Optional[QualificationScore]:
        ctx = self._load_insights_ctx()
        raw = self.qualifier.qualify(
            prospect.model_dump_json(indent=2), product_name, value_proposition, call_notes, ctx
        )
        return _qual_from_json(raw, prospect)

    def nurture_only(
        self,
        prospects: List[Prospect],
        product_name: str,
        value_proposition: str,
        duration_days: int,
    ) -> List[NurtureSequence]:
        ctx = self._load_insights_ctx()
        sequences = []
        for p in prospects:
            raw = self.nurture.build_sequence(
                p.model_dump_json(indent=2), product_name, value_proposition, duration_days, ctx
            )
            seq = _nurture_from_json(raw, p, duration_days)
            if seq:
                sequences.append(seq)
        return sequences

    def propose_only(self, req: ProposalRequest) -> Optional[SalesProposal]:
        ctx = self._load_insights_ctx()
        cases = "\n".join(req.case_study_snippets)
        raw = self.proposal.write(
            req.prospect.model_dump_json(indent=2),
            req.product_name,
            req.value_proposition,
            req.annual_cost_usd,
            req.discovery_notes,
            cases,
            req.company_context,
            ctx,
        )
        return _proposal_from_json(raw, req.prospect, req.annual_cost_usd)

    def coach_only(
        self, prospects: List[Prospect], product_name: str, pipeline_context: str
    ) -> Optional[PipelineCoachingReport]:
        ctx = self._load_insights_ctx()
        prospects_json = json.dumps([p.model_dump() for p in prospects], indent=2)
        raw = self.coach.review(prospects_json, product_name, pipeline_context, ctx)
        return _coaching_from_json(raw, len(prospects))

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
        companies_raw = self.prospector.prospect_companies(
            icp_json,
            request.product_name,
            request.value_proposition,
            companies_requested,
            request.company_context,
            ctx,
        )
        companies = _prospects_from_json(companies_raw)
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
                raw = self.decision_maker_mapper.map_contacts(
                    company.model_dump_json(indent=2),
                    icp_json,
                    request.product_name,
                    request.value_proposition,
                    request.max_per_company,
                    ctx,
                )
                return _decision_makers_from_json(raw, company)
            except Exception:
                logger.exception(
                    "decision-maker mapping failed for company %s", company.company_name
                )
                return []

        with ThreadPoolExecutor(max_workers=8) as pool:
            for result in pool.map(_map_one, companies):
                mapped.extend(result)

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
                raw = self.dossier_builder.build(
                    p.model_dump_json(indent=2),
                    request.product_name,
                    request.value_proposition,
                    ctx,
                )
                return p, _dossier_from_json(raw, p)
            except Exception:
                logger.exception("dossier building failed for prospect %s", p.id)
                return p, None

        dossiers: dict[str, ProspectDossier] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_build_one, p) for p in final_prospects]
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
