"""SalesPodOrchestrator — coordinates all sales team agents through the pipeline."""

from __future__ import annotations

import json
import logging
from typing import Callable, List, Optional

from .agents import (
    CloserAgent,
    DiscoveryAgent,
    LeadQualifierAgent,
    NurtureAgent,
    OutreachAgent,
    ProposalAgent,
    ProspectorAgent,
    SalesCoachAgent,
)
from .models import (
    BANTScore,
    ClosingStrategy,
    DiscoveryPlan,
    IdealCustomerProfile,
    MEDDICScore,
    NurtureSequence,
    ObjectionHandler,
    OutreachSequence,
    PipelineCoachingReport,
    PipelineStage,
    Prospect,
    ProposalRequest,
    QualificationScore,
    ROIModel,
    SalesPipelineRequest,
    SalesPipelineResult,
    SalesProposal,
    SPINQuestions,
)

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


def _nurture_from_json(raw: str, prospect: Prospect, duration_days: int) -> Optional[NurtureSequence]:
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


def _proposal_from_json(raw: str, prospect: Prospect, annual_cost: float) -> Optional[SalesProposal]:
    data = _parse_json(raw, {})
    if not isinstance(data, dict):
        return None
    try:
        roi_data = data.get("roi_model", {})
        roi = ROIModel(
            annual_cost_usd=float(roi_data.get("annual_cost_usd", annual_cost)),
            estimated_annual_benefit_usd=float(roi_data.get("estimated_annual_benefit_usd", annual_cost * 2.5)),
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
                raw = self.prospector.prospect(icp_json, product, vp, request.max_prospects, ctx)
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
                    p.model_dump_json(indent=2), product, vp, cases, ctx
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
                raw = self.qualifier.qualify(p.model_dump_json(indent=2), product, vp, "")
                score = _qual_from_json(raw, p)
                if score:
                    qualified.append(score)
            result.qualified_leads = qualified
            update("qualification", 50)

        # Determine which prospects advance vs. go to nurture
        if qualified:
            advance = [q for q in qualified if q.recommended_action.lower().startswith("advance")]
            nurture_prospects = [
                q.prospect for q in qualified
                if not q.recommended_action.lower().startswith("advance")
                and not q.recommended_action.lower().startswith("disqualify")
            ]
        else:
            # No qualification ran — advance all prospects
            advance = []
            nurture_prospects = []
            qualified_all = prospects  # type: ignore[assignment]
        qualified_prospects = [q.prospect for q in advance] if advance else prospects

        # ------------------------------------------------------------------
        # Stage 4: Nurturing
        # ------------------------------------------------------------------
        if self._should_run(PipelineStage.NURTURING, entry) and nurture_prospects:
            update("nurturing", 55)
            logger.info("Sales pod [%s]: nurturing %d prospects", job_id, len(nurture_prospects))
            nurture_seqs: List[NurtureSequence] = []
            for p in nurture_prospects:
                raw = self.nurture.build_sequence(p.model_dump_json(indent=2), product, vp, 90)
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
            logger.info("Sales pod [%s]: discovery stage for %d prospects", job_id, len(qualified_prospects))
            plans: List[DiscoveryPlan] = []
            for p in qualified_prospects:
                qual_json = "{}"
                for q in qualified:
                    if q.prospect.company_name == p.company_name:
                        qual_json = q.model_dump_json(indent=2)
                        break
                raw = self.discovery.prepare(p.model_dump_json(indent=2), qual_json, product, vp)
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
            logger.info("Sales pod [%s]: proposal stage for %d prospects", job_id, len(qualified_prospects))
            proposals: List[SalesProposal] = []
            annual_cost = 25000.0  # Default; real requests should supply per-prospect pricing
            for p in qualified_prospects:
                raw = self.proposal.write(
                    p.model_dump_json(indent=2), product, vp, annual_cost, "", cases, ctx
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
                    p.model_dump_json(indent=2), prop_json, product, vp
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
        raw = self.coach.review(prospects_json, product, "")
        coaching = _coaching_from_json(raw, len(prospects))
        result.coaching_report = coaching

        # Summary
        result.summary = (
            f"Sales pod completed pipeline from '{entry.value}' stage. "
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

    # ------------------------------------------------------------------
    # Convenience single-stage methods (used by standalone API endpoints)
    # ------------------------------------------------------------------

    def prospect_only(
        self,
        icp: IdealCustomerProfile,
        product_name: str,
        value_proposition: str,
        max_prospects: int,
        company_context: str,
    ) -> List[Prospect]:
        raw = self.prospector.prospect(
            icp.model_dump_json(indent=2), product_name, value_proposition, max_prospects, company_context
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
        cases = "\n".join(case_study_snippets)
        sequences = []
        for p in prospects:
            raw = self.outreach.generate_sequence(
                p.model_dump_json(indent=2), product_name, value_proposition, cases, company_context
            )
            seq = _outreach_from_json(raw, p)
            if seq:
                sequences.append(seq)
        return sequences

    def qualify_only(
        self, prospect: Prospect, product_name: str, value_proposition: str, call_notes: str
    ) -> Optional[QualificationScore]:
        raw = self.qualifier.qualify(
            prospect.model_dump_json(indent=2), product_name, value_proposition, call_notes
        )
        return _qual_from_json(raw, prospect)

    def nurture_only(
        self,
        prospects: List[Prospect],
        product_name: str,
        value_proposition: str,
        duration_days: int,
    ) -> List[NurtureSequence]:
        sequences = []
        for p in prospects:
            raw = self.nurture.build_sequence(
                p.model_dump_json(indent=2), product_name, value_proposition, duration_days
            )
            seq = _nurture_from_json(raw, p, duration_days)
            if seq:
                sequences.append(seq)
        return sequences

    def propose_only(self, req: ProposalRequest) -> Optional[SalesProposal]:
        cases = "\n".join(req.case_study_snippets)
        raw = self.proposal.write(
            req.prospect.model_dump_json(indent=2),
            req.product_name,
            req.value_proposition,
            req.annual_cost_usd,
            req.discovery_notes,
            cases,
            req.company_context,
        )
        return _proposal_from_json(raw, req.prospect, req.annual_cost_usd)

    def coach_only(
        self, prospects: List[Prospect], product_name: str, pipeline_context: str
    ) -> Optional[PipelineCoachingReport]:
        prospects_json = json.dumps([p.model_dump() for p in prospects], indent=2)
        raw = self.coach.review(prospects_json, product_name, pipeline_context)
        return _coaching_from_json(raw, len(prospects))
