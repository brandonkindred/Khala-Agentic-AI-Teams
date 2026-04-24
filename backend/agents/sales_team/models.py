"""Pydantic models for the AI Sales Team pipeline."""

from __future__ import annotations

import logging
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

logger = logging.getLogger(__name__)

# Dossiers below this confidence drop to the company_soft_opener angle.
# Previously lived in ``agents.py``; moved here so the validator that enforces
# the confidence gate doesn't pull in agent imports.
PERSONALIZATION_CONFIDENCE_THRESHOLD = 0.6

# ---------------------------------------------------------------------------
# Type aliases used by the outreach payload
# ---------------------------------------------------------------------------

PersonalizationGrade = Literal["high", "medium", "low", "fallback"]
OutreachAngle = Literal[
    "trigger_event",
    "thought_leadership",
    "mutual_connection",
    "peer_proof",
    "company_soft_opener",
]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PipelineStage(str, Enum):
    PROSPECTING = "prospecting"
    OUTREACH = "outreach"
    QUALIFICATION = "qualification"
    NURTURING = "nurturing"
    DISCOVERY = "discovery"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


class OutreachChannel(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    LINKEDIN = "linkedin"
    VIDEO = "video"


class CloseType(str, Enum):
    ASSUMPTIVE = "assumptive"
    SUMMARY = "summary"
    URGENCY = "urgency"
    ALTERNATIVE_CHOICE = "alternative_choice"
    SHARP_ANGLE = "sharp_angle"
    FEEL_FELT_FOUND = "feel_felt_found"


class ForecastCategory(str, Enum):
    PIPELINE = "pipeline"
    BEST_CASE = "best_case"
    COMMIT = "commit"
    CLOSED = "closed"
    OMITTED = "omitted"


# ---------------------------------------------------------------------------
# ICP & Prospect models
# ---------------------------------------------------------------------------


class IdealCustomerProfile(BaseModel):
    """Defines the Ideal Customer Profile used to score and filter prospects."""

    industry: List[str] = Field(
        default_factory=list, description="Target industries, e.g. ['SaaS', 'FinTech']"
    )
    company_size_min: int = Field(default=10, description="Minimum employee count")
    company_size_max: int = Field(default=5000, description="Maximum employee count")
    job_titles: List[str] = Field(
        default_factory=list, description="Target buyer titles, e.g. ['VP Sales', 'CRO']"
    )
    pain_points: List[str] = Field(
        default_factory=list, description="Core pains the product solves"
    )
    budget_range_usd: str = Field(default="$10k–$100k/yr", description="Expected ACV range")
    geographic_focus: List[str] = Field(
        default_factory=list, description="Target regions or countries"
    )
    tech_stack_keywords: List[str] = Field(
        default_factory=list, description="Technologies the prospect likely uses"
    )
    disqualifying_traits: List[str] = Field(
        default_factory=list, description="Traits that rule out a prospect"
    )


class Prospect(BaseModel):
    """Raw lead identified during prospecting — not yet contacted."""

    id: str = Field(
        default="",
        description="Stable prospect identifier ('prs_<uuid12>'); assigned by the orchestrator when empty",
    )
    company_name: str
    website: Optional[str] = None
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    contact_email: Optional[str] = None
    linkedin_url: Optional[str] = None
    company_size_estimate: Optional[str] = None
    industry: Optional[str] = None
    icp_match_score: float = Field(default=0.0, ge=0.0, le=1.0, description="0–1 ICP fit score")
    research_notes: str = Field(default="", description="Key intel gathered during prospecting")
    trigger_events: List[str] = Field(
        default_factory=list, description="Recent events making them likely to buy now"
    )
    dossier_id: Optional[str] = Field(
        default=None,
        description="ID of the ProspectDossier that provides deep research on this prospect",
    )


# ---------------------------------------------------------------------------
# Dossier models — deep-research artifact for a single prospect
# ---------------------------------------------------------------------------


class CareerRole(BaseModel):
    """A single role in a prospect's career history."""

    company: str
    title: str
    start: Optional[str] = Field(default=None, description="e.g. '2021' or '2021-06'")
    end: Optional[str] = Field(default=None, description="None = current role")
    summary: Optional[str] = None


class PublicWorkItem(BaseModel):
    """A publicly visible piece of work by the prospect.

    Covers writing, research, talks, podcasts, OSS contributions, patents, interviews.
    """

    kind: str = Field(
        ...,
        description="article | talk | paper | podcast | oss | patent | interview | other",
    )
    title: str
    url: Optional[str] = None
    venue: Optional[str] = Field(
        default=None, description="Conference, publication, podcast, or platform name"
    )
    date: Optional[str] = None
    summary: Optional[str] = None


class DecisionMakerSignal(BaseModel):
    """A single piece of evidence that this prospect has buying authority."""

    signal: str = Field(
        ...,
        description=(
            "Short handle for the signal, e.g. 'reports_directly_to_ceo', "
            "'owns_budget_for_data_tooling', 'sole_signatory_on_vendor_page'"
        ),
    )
    evidence_url: Optional[str] = Field(
        default=None, description="Public URL that supports this signal"
    )
    strength: str = Field(default="medium", description="weak | medium | strong")


class ProspectDossier(BaseModel):
    """Deep-research profile of a single prospect.

    Assembled from publicly available sources. Every non-trivial claim should
    have a corresponding URL in ``sources``. Unknown fields stay empty rather
    than being fabricated.
    """

    dossier_id: str = Field(
        default="",
        description="Stable dossier identifier ('dsr_<uuid12>'); assigned by the store on write",
    )
    prospect_id: str = Field(..., description="Links back to Prospect.id")
    generated_at: str = Field(default="", description="ISO-8601 UTC timestamp")

    # Identity
    full_name: str
    current_title: str
    current_company: str
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    personal_site: Optional[str] = None
    other_social: List[str] = Field(
        default_factory=list,
        description="Public profile URLs (Twitter/X, GitHub, Substack, Mastodon, etc.)",
    )

    # Narrative
    executive_summary: str = Field(
        default="",
        description="3–5 sentence distillation: who they are and why they matter for this sale",
    )
    career_history: List[CareerRole] = Field(default_factory=list)
    education: List[str] = Field(default_factory=list)

    # Thought-leadership
    publications: List[PublicWorkItem] = Field(
        default_factory=list,
        description="Writing, research, talks, podcasts, OSS, patents, interviews",
    )
    topics_of_interest: List[str] = Field(default_factory=list)
    stated_beliefs: List[str] = Field(
        default_factory=list,
        description="Quotes or positions the prospect has taken publicly",
    )

    # Buying signals
    decision_maker_signals: List[DecisionMakerSignal] = Field(default_factory=list)
    recent_activity: List[str] = Field(
        default_factory=list,
        description="Recent posts, job moves, speaking engagements, or company events",
    )
    trigger_events: List[str] = Field(default_factory=list)

    # Outreach helpers
    conversation_hooks: List[str] = Field(
        default_factory=list,
        description="3–7 angles tying the product to this specific person",
    )
    mutual_connection_angles: List[str] = Field(
        default_factory=list,
        description="Shared companies, schools, communities, or past collaborators",
    )
    personalization_tokens: dict = Field(
        default_factory=dict,
        description="Ready-to-merge fields for outreach templates (e.g. {'first_name': 'Jane'})",
    )

    # Provenance
    sources: List[str] = Field(
        default_factory=list, description="Public URLs consulted while building this dossier"
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Overall confidence that the dossier identifies the right person and is factually "
            "grounded. Decreases sharply if fewer than 3 independent sources corroborate identity."
        ),
    )
    notes: str = Field(default="")


# ---------------------------------------------------------------------------
# Outreach models
# ---------------------------------------------------------------------------


class EvidenceCitation(BaseModel):
    """Dossier-rooted evidence backing a personalization claim in outreach copy."""

    claim: str = Field(
        ...,
        description="The personalized statement the email makes, e.g. 'saw your talk on distributed tracing at QCon'",
    )
    dossier_field: str = Field(
        ...,
        description="Path inside ProspectDossier that supports the claim, e.g. 'publications[2]' or 'trigger_events[0]'",
    )
    source_url: Optional[str] = Field(
        default=None,
        description="URL from dossier.sources that supports the claim; None for summary-level claims with no URL",
    )
    strength: Literal["weak", "medium", "strong"] = Field(default="medium")


class EmailTouch(BaseModel):
    """A single email in a multi-touch cold outreach sequence."""

    day: int = Field(..., description="Day in the sequence to send this email (0-indexed)")
    subject_line: str
    body: str
    personalization_tokens: List[str] = Field(
        default_factory=list, description="Merge fields to fill before sending"
    )
    call_to_action: str = Field(default="", description="Specific ask at the end of this touch")
    evidence_citations: List[EvidenceCitation] = Field(
        default_factory=list,
        description="Dossier-rooted evidence for every personalization claim in this touch",
    )

    @field_validator("evidence_citations", mode="after")
    @classmethod
    def _strip_unverified_citations(
        cls, value: List[EvidenceCitation], info: ValidationInfo
    ) -> List[EvidenceCitation]:
        """Drop citations whose source_url is not in the dossier's verified URL set.

        The allowed URL set is passed via ``ValidationInfo.context`` under
        ``dossier_source_urls`` (a set of strings). If no context is given,
        the list is returned unchanged — this keeps EmailTouch usable outside
        the outreach flow (e.g. for deserialising persisted data).

        When a citation is stripped, the flag
        ``context["citations_stripped"] = True`` is set so the
        :class:`OutreachVariant` model_validator can downgrade the
        ``personalization_grade`` to ``"low"``.
        """
        if info.context is None:
            return value
        allowed = info.context.get("dossier_source_urls")
        if allowed is None:
            return value
        verified: List[EvidenceCitation] = []
        stripped_any = False
        for cit in value:
            if cit.source_url and cit.source_url not in allowed:
                logger.warning(
                    "sales.outreach.citation_unverified url=%s",
                    cit.source_url,
                )
                stripped_any = True
                continue
            verified.append(cit)
        if stripped_any:
            # Mutate the shared context so the enclosing OutreachVariant can react.
            info.context["citations_stripped"] = True
        return verified


class OutreachVariant(BaseModel):
    """One angle-specific version of a prospect's outreach sequence.

    Every variant uses a different angle so downstream A/B tracking can
    attribute reply rate to angle type, not just subject-line text.
    """

    angle: OutreachAngle = Field(
        ...,
        description="Personalization angle used for this variant",
    )
    email_sequence: List[EmailTouch] = Field(default_factory=list)
    call_script: str = Field(default="", description="Cold-call opener and talk track")
    linkedin_message: str = Field(default="", description="Connection request / InMail copy")
    rationale: str = Field(
        default="", description="Why this angle was picked and why it is expected to land"
    )
    personalization_grade: PersonalizationGrade = Field(
        default="medium",
        description=(
            "high  = grounded in strong dossier signal with source URL; "
            "medium = grounded in at least one cited dossier field; "
            "low    = citation verifier stripped an unverified URL; "
            "fallback = company_soft_opener, no person-level claim"
        ),
    )

    @model_validator(mode="after")
    def _enforce_personalization_contract(self, info: ValidationInfo) -> "OutreachVariant":
        """Grade-enforcement rules lifted from the old ``_outreach_from_json`` glue.

        1. If the EmailTouch validator stripped any citation (signalled via
           ``context["citations_stripped"]``), downgrade ``high``/``medium`` to
           ``low``.
        2. If the variant is not the ``company_soft_opener`` angle and its
           Day-1 email has zero evidence citations, force the grade to
           ``"fallback"`` — the model failed the Personalization Contract.
        """
        if info.context and info.context.get("citations_stripped"):
            if self.personalization_grade in ("high", "medium"):
                self.personalization_grade = "low"
            # Clear the flag so it doesn't leak to the next variant being
            # validated in the same OutreachVariantList.
            info.context["citations_stripped"] = False

        if self.angle != "company_soft_opener" and self.email_sequence:
            day1 = next(
                (t for t in self.email_sequence if t.day <= 1),
                self.email_sequence[0],
            )
            if not day1.evidence_citations:
                logger.warning("sales.outreach.missing_citations angle=%s", self.angle)
                self.personalization_grade = "fallback"
        return self


class OutreachSequence(BaseModel):
    """All generated variants for a single prospect's outreach."""

    prospect: Prospect
    dossier_id: str = Field(..., description="ProspectDossier this sequence was grounded in")
    dossier_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Snapshot of dossier.confidence at generation time"
    )
    variants: List[OutreachVariant] = Field(
        default_factory=list,
        description="One entry per angle; every entry uses a different angle",
    )

    @model_validator(mode="after")
    def _enforce_confidence_gate(self) -> "OutreachSequence":
        """Drop non-``company_soft_opener`` variants when dossier confidence is low.

        Mirrors the Enforcement-3 rule that used to live in
        ``orchestrator._outreach_from_json``. Policy: when the dossier isn't
        trustworthy enough to back person-level personalization, the sequence
        may only carry company-level soft-opener variants. Does **not**
        synthesise a fallback variant — that remains the orchestrator's job
        (a validator shouldn't fabricate data).
        """
        if self.dossier_confidence < PERSONALIZATION_CONFIDENCE_THRESHOLD:
            non_soft = [v for v in self.variants if v.angle != "company_soft_opener"]
            if non_soft:
                logger.warning(
                    "sales.outreach.confidence_override dossier_id=%s confidence=%.2f dropped_angles=%s",
                    self.dossier_id,
                    self.dossier_confidence,
                    [v.angle for v in non_soft],
                )
            self.variants = [v for v in self.variants if v.angle == "company_soft_opener"]
        return self


# ---------------------------------------------------------------------------
# LLM response wrapper schemas
#
# ``generate_structured`` requires a Pydantic BaseModel at the top level. Many
# sales agent prompts naturally want to return a list (prospects, decision
# makers, variants). These wrappers give those responses a single-object root
# that the shared llm_service helper can validate.
# ---------------------------------------------------------------------------


class ProspectList(BaseModel):
    """LLM wrapper for ProspectorAgent output (prospects or companies)."""

    prospects: List[Prospect] = Field(default_factory=list)


class DecisionMakerEntry(BaseModel):
    """Shape returned by DecisionMakerMapperAgent for each identified contact."""

    contact_name: str
    contact_title: str = ""
    linkedin_url: Optional[str] = None
    contact_email: Optional[str] = None  # always null in practice; never fabricated
    decision_maker_rationale: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class DecisionMakerList(BaseModel):
    """LLM wrapper for DecisionMakerMapperAgent output."""

    contacts: List[DecisionMakerEntry] = Field(default_factory=list)


class OutreachVariantList(BaseModel):
    """LLM wrapper for OutreachAgent output — raw variants before wrapping in OutreachSequence.

    The orchestrator constructs the final :class:`OutreachSequence` after this
    response validates, attaching ``prospect``, ``dossier_id``, and
    ``dossier_confidence`` which the LLM should not fabricate.
    """

    variants: List[OutreachVariant] = Field(default_factory=list)


class QualificationScoreBody(BaseModel):
    """LLM wrapper for LeadQualifierAgent — the per-prospect body without the prospect field."""

    bant: BANTScore
    meddic: MEDDICScore
    overall_score: float = Field(default=0.5, ge=0.0, le=1.0)
    value_creation_level: int = Field(default=2, ge=1, le=4)
    recommended_action: str = "nurture"
    disqualification_reason: Optional[str] = None
    qualification_notes: str = ""


class NurtureSequenceBody(BaseModel):
    """LLM wrapper for NurtureAgent — body without the prospect field."""

    duration_days: int = 90
    touchpoints: List["NurtureTouchpoint"] = Field(default_factory=list)
    re_engagement_triggers: List[str] = Field(default_factory=list)
    content_recommendations: List[str] = Field(default_factory=list)


class DiscoveryPlanBody(BaseModel):
    """LLM wrapper for DiscoveryAgent — body without the prospect field."""

    spin_questions: "SPINQuestions"
    challenger_insight: str = ""
    demo_agenda: List[str] = Field(default_factory=list)
    expected_objections: List[str] = Field(default_factory=list)
    success_criteria_for_call: str = ""


class SalesProposalBody(BaseModel):
    """LLM wrapper for ProposalAgent — body without the prospect field."""

    executive_summary: str = ""
    situation_analysis: str = ""
    proposed_solution: str = ""
    roi_model: "ROIModel"
    investment_table: str = ""
    implementation_timeline: str = ""
    risk_mitigation: str = ""
    next_steps: List[str] = Field(default_factory=list)
    custom_sections: List["ProposalSection"] = Field(default_factory=list)


class ClosingStrategyBody(BaseModel):
    """LLM wrapper for CloserAgent — body without the prospect field."""

    recommended_close_technique: "CloseType"
    close_script: str = ""
    objection_handlers: List["ObjectionHandler"] = Field(default_factory=list)
    urgency_framing: str = ""
    walk_away_criteria: str = ""
    emotional_intelligence_notes: str = ""


# ---------------------------------------------------------------------------
# Qualification models
# ---------------------------------------------------------------------------


class BANTScore(BaseModel):
    """BANT qualification framework scores."""

    budget: int = Field(..., ge=0, le=10, description="Does the prospect have budget? 0–10")
    authority: int = Field(..., ge=0, le=10, description="Is the contact a decision-maker? 0–10")
    need: int = Field(..., ge=0, le=10, description="Is there a confirmed, urgent need? 0–10")
    timeline: int = Field(
        ..., ge=0, le=10, description="Is there a defined purchase timeline? 0–10"
    )


class MEDDICScore(BaseModel):
    """MEDDIC qualification framework signals."""

    metrics_identified: bool = Field(
        default=False, description="Quantified business impact defined"
    )
    economic_buyer_known: bool = Field(default=False)
    decision_criteria_understood: bool = Field(default=False)
    decision_process_mapped: bool = Field(default=False)
    identify_pain: bool = Field(default=False, description="Root pain confirmed")
    champion_found: bool = Field(default=False, description="Internal champion identified")


class QualificationScore(BaseModel):
    """Combined BANT + MEDDIC qualification result for a lead."""

    prospect: Prospect
    bant: BANTScore
    meddic: MEDDICScore
    overall_score: float = Field(..., ge=0.0, le=1.0, description="Weighted composite 0–1")
    value_creation_level: int = Field(..., ge=1, le=4, description="Iannarino Level 1–4 value tier")
    recommended_action: str = Field(..., description="Next step: advance, nurture, or disqualify")
    disqualification_reason: Optional[str] = None
    qualification_notes: str = Field(default="")


# ---------------------------------------------------------------------------
# Nurture models
# ---------------------------------------------------------------------------


class NurtureTouchpoint(BaseModel):
    """A single step in a nurture sequence."""

    day: int
    channel: OutreachChannel
    content_type: str = Field(
        ..., description="e.g. 'case study email', 'ROI calculator link', 'check-in call'"
    )
    message: str
    goal: str = Field(default="", description="What this touch is designed to accomplish")


class NurtureSequence(BaseModel):
    """Long-cycle follow-up plan for leads not ready to buy yet."""

    prospect: Prospect
    duration_days: int = Field(default=90)
    touchpoints: List[NurtureTouchpoint] = Field(default_factory=list)
    re_engagement_triggers: List[str] = Field(
        default_factory=list,
        description="Events that should pull the lead back into active pipeline",
    )
    content_recommendations: List[str] = Field(
        default_factory=list,
        description="Blog posts, case studies, or assets to share over the sequence",
    )


# ---------------------------------------------------------------------------
# Discovery models
# ---------------------------------------------------------------------------


class SPINQuestions(BaseModel):
    """SPIN Selling question set for discovery calls."""

    situation: List[str] = Field(default_factory=list, description="Questions about current state")
    problem: List[str] = Field(default_factory=list, description="Questions that surface pain")
    implication: List[str] = Field(
        default_factory=list, description="Questions that amplify consequences of inaction"
    )
    need_payoff: List[str] = Field(
        default_factory=list, description="Questions that surface the value of solving the problem"
    )


class DiscoveryPlan(BaseModel):
    """Prep pack for a discovery call or product demo."""

    prospect: Prospect
    spin_questions: SPINQuestions
    challenger_insight: str = Field(
        default="",
        description="Provocative insight to open the call and reframe the prospect's thinking",
    )
    demo_agenda: List[str] = Field(default_factory=list, description="Ordered demo talking points")
    expected_objections: List[str] = Field(default_factory=list)
    success_criteria_for_call: str = Field(
        default="", description="What a successful call looks like"
    )


# ---------------------------------------------------------------------------
# Proposal models
# ---------------------------------------------------------------------------


class ROIModel(BaseModel):
    """Simple ROI / payback calculation for a proposal."""

    annual_cost_usd: float
    estimated_annual_benefit_usd: float
    payback_months: float
    roi_percentage: float
    assumptions: List[str] = Field(default_factory=list)


class ProposalSection(BaseModel):
    heading: str
    content: str


class SalesProposal(BaseModel):
    """Full written sales proposal."""

    prospect: Prospect
    executive_summary: str
    situation_analysis: str
    proposed_solution: str
    roi_model: ROIModel
    investment_table: str = Field(default="", description="Pricing and packaging breakdown")
    implementation_timeline: str = Field(default="")
    risk_mitigation: str = Field(default="")
    next_steps: List[str] = Field(default_factory=list)
    custom_sections: List[ProposalSection] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Closing models
# ---------------------------------------------------------------------------


class ObjectionHandler(BaseModel):
    objection: str
    response: str
    feel_felt_found: Optional[str] = None


class ClosingStrategy(BaseModel):
    """Closing pack grounded in Zig Ziglar and Jeb Blount's Sales EQ."""

    prospect: Prospect
    recommended_close_technique: CloseType
    close_script: str
    objection_handlers: List[ObjectionHandler] = Field(default_factory=list)
    urgency_framing: str = Field(
        default="", description="Legitimate urgency lever to accelerate decision"
    )
    walk_away_criteria: str = Field(
        default="", description="Conditions under which to disengage from the deal"
    )
    emotional_intelligence_notes: str = Field(
        default="",
        description="Sales EQ guidance: acknowledge emotions before logic",
    )


# ---------------------------------------------------------------------------
# Coaching models
# ---------------------------------------------------------------------------


class DealRiskSignal(BaseModel):
    signal: str
    severity: str = Field(..., description="low | medium | high")
    recommended_action: str


class PipelineCoachingReport(BaseModel):
    """Gong Labs-style pipeline review and coaching output."""

    prospects_reviewed: int
    deal_risk_signals: List[DealRiskSignal] = Field(default_factory=list)
    talk_listen_ratio_advice: str = Field(default="")
    velocity_insights: str = Field(
        default="", description="Pipeline velocity and stage duration analysis"
    )
    forecast_category: ForecastCategory = ForecastCategory.PIPELINE
    top_priority_deals: List[str] = Field(default_factory=list)
    recommended_next_actions: List[str] = Field(default_factory=list)
    coaching_summary: str = Field(default="")


# ---------------------------------------------------------------------------
# Pipeline-level request/response models
# ---------------------------------------------------------------------------


class SalesPipelineRequest(BaseModel):
    """Top-level request to run the full sales pod pipeline."""

    product_name: str = Field(..., min_length=1, max_length=200)
    value_proposition: str = Field(..., min_length=10, max_length=5000)
    icp: IdealCustomerProfile
    entry_stage: PipelineStage = PipelineStage.PROSPECTING
    max_prospects: int = Field(
        default=5,
        ge=1,
        le=100,
        description=(
            "Max leads to generate. The legacy flat pipeline typically uses ≤20; the deep-research "
            "endpoint raises this up to 100."
        ),
    )
    existing_prospects: List[Prospect] = Field(
        default_factory=list,
        description="Pre-existing leads to skip prospecting (used when entry_stage != PROSPECTING)",
    )
    company_context: str = Field(
        default="",
        max_length=5000,
        description="About your company: size, mission, differentiators",
    )
    case_study_snippets: List[str] = Field(
        default_factory=list,
        description="1–3 sentence customer wins to use in outreach/proposals",
    )


class ProspectingRequest(BaseModel):
    """Standalone prospecting request."""

    icp: IdealCustomerProfile
    product_name: str
    value_proposition: str
    max_prospects: int = Field(default=5, ge=1, le=100)
    company_context: str = Field(default="", max_length=5000)


class DeepResearchRequest(BaseModel):
    """Run a deep-research prospecting pass — produces a top-N list with full dossiers.

    Unlike :class:`ProspectingRequest` which returns a flat batch of prospects,
    this request drives a company→decision-maker→dossier pipeline and enforces
    a per-company cap on how many prospects appear in the final ranked list.
    """

    product_name: str = Field(..., min_length=1, max_length=200)
    value_proposition: str = Field(..., min_length=10, max_length=5000)
    icp: IdealCustomerProfile
    target_prospects: int = Field(
        default=100,
        ge=10,
        le=100,
        description="Number of prospects to return in the ranked list",
    )
    max_per_company: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "Hard cap on how many prospects from the same company may appear in the final list. "
            "Default 2: aligns with the 'no more than 2 prospects per company' rule."
        ),
    )
    company_context: str = Field(default="", max_length=5000)


class ProspectListEntry(BaseModel):
    """A single entry in a deep-research top-N list.

    Holds the ranked prospect plus a reference to the full dossier so
    consumers can fetch the dossier independently via its API endpoint.
    """

    rank: int = Field(..., ge=1, description="1-based rank in the top-N list")
    prospect: Prospect
    dossier_id: str = Field(..., description="ID of this prospect's ProspectDossier")
    dossier_url: str = Field(
        ...,
        description="Relative API path for retrieving the dossier, e.g. /api/sales/dossiers/<id>",
    )


class DeepResearchResult(BaseModel):
    """Ranked top-N prospect list + metadata produced by a deep-research run."""

    list_id: str = Field(default="", description="Stable list identifier ('plst_<uuid12>')")
    product_name: str
    generated_at: str = Field(default="", description="ISO-8601 UTC timestamp")
    total_prospects: int = Field(..., ge=0)
    companies_represented: int = Field(..., ge=0)
    entries: List[ProspectListEntry] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description="Non-fatal issues from the run (e.g. shortfalls, dropped duplicates)",
    )


class OutreachRequest(BaseModel):
    """Generate outreach sequences for a list of prospects."""

    prospects: List[Prospect]
    product_name: str
    value_proposition: str
    case_study_snippets: List[str] = Field(default_factory=list)
    company_context: str = Field(default="", max_length=5000)


class QualificationRequest(BaseModel):
    """Qualify a single prospect."""

    prospect: Prospect
    product_name: str
    value_proposition: str
    call_notes: str = Field(default="", description="Notes from any discovery call or conversation")


class NurtureRequest(BaseModel):
    """Build a nurture sequence for prospects not ready to buy."""

    prospects: List[Prospect]
    product_name: str
    value_proposition: str
    duration_days: int = Field(default=90, ge=7, le=365)


class ProposalRequest(BaseModel):
    """Generate a sales proposal for a qualified opportunity."""

    prospect: Prospect
    product_name: str
    value_proposition: str
    annual_cost_usd: float = Field(..., gt=0)
    discovery_notes: str = Field(default="", description="Notes from discovery call")
    case_study_snippets: List[str] = Field(default_factory=list)
    company_context: str = Field(default="", max_length=5000)


class CoachingRequest(BaseModel):
    """Request a pipeline coaching report."""

    prospects: List[Prospect]
    product_name: str
    pipeline_context: str = Field(
        default="", description="Additional context about deal status, stage durations, etc."
    )


# ---------------------------------------------------------------------------
# Outcome tracking models
# ---------------------------------------------------------------------------


class OutcomeResult(str, Enum):
    """What happened at a specific pipeline stage for a prospect."""

    CONVERTED = "converted"  # Moved forward to next stage
    STALLED = "stalled"  # No response / no movement
    OBJECTION = "objection"  # Objection raised; outcome pending
    DISQUALIFIED = "disqualified"  # Explicitly ruled out
    WON = "won"  # Deal closed won
    LOST = "lost"  # Deal closed lost


class DealResult(str, Enum):
    """Final close result for a deal — only valid values for DealOutcome records."""

    WON = "won"
    LOST = "lost"


class StageOutcome(BaseModel):
    """Records what happened at a single pipeline stage for one prospect."""

    outcome_id: str = Field(default="", description="UUID assigned by the store on write")
    recorded_at: str = Field(default="", description="ISO-8601 UTC timestamp")
    pipeline_job_id: Optional[str] = None
    company_name: str
    industry: Optional[str] = None
    stage: PipelineStage
    outcome: OutcomeResult
    icp_match_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    qualification_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Stage-specific details (open to extension)
    email_touch_number: Optional[int] = Field(
        default=None, description="Which email touch got the reply (outreach stage)"
    )
    subject_line_used: Optional[str] = Field(
        default=None, description="Subject line that drove the reply (outreach stage)"
    )
    objection_text: Optional[str] = Field(default=None, description="Objection raised, if any")
    close_technique_used: Optional[CloseType] = Field(
        default=None, description="Close technique attempted (negotiation stage)"
    )
    notes: str = Field(default="")


class DealOutcome(BaseModel):
    """Full deal outcome — recorded when a deal reaches closed_won or closed_lost."""

    outcome_id: str = Field(default="", description="UUID assigned by the store on write")
    recorded_at: str = Field(default="", description="ISO-8601 UTC timestamp")
    pipeline_job_id: Optional[str] = None
    company_name: str
    industry: Optional[str] = None
    deal_size_usd: Optional[float] = Field(default=None, gt=0)
    final_stage_reached: PipelineStage
    result: DealResult
    loss_reason: Optional[str] = Field(
        default=None,
        description="Why the deal was lost (price, competitor, timing, no champion, etc.)",
    )
    win_factor: Optional[str] = Field(default=None, description="Primary reason the deal was won")
    close_technique_used: Optional[CloseType] = None
    objections_raised: List[str] = Field(default_factory=list)
    stages_completed: List[PipelineStage] = Field(default_factory=list)
    icp_match_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    qualification_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sales_cycle_days: Optional[int] = Field(
        default=None, description="Total days from first contact to close"
    )
    notes: str = Field(default="")


class LearningInsights(BaseModel):
    """Extracted patterns from historical outcomes — injected into agent prompts."""

    total_outcomes_analyzed: int = 0
    win_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="Fraction of deals won")
    stage_conversion_rates: dict = Field(
        default_factory=dict, description="stage_name → conversion % to next stage"
    )
    top_performing_industries: List[str] = Field(
        default_factory=list, description="Industries with highest win rates"
    )
    top_icp_signals: List[str] = Field(
        default_factory=list, description="ICP traits / trigger events that correlated with wins"
    )
    best_outreach_angles: List[str] = Field(
        default_factory=list,
        description="Subject line patterns or email angles with high reply rates",
    )
    common_objections: List[str] = Field(
        default_factory=list, description="Objections that appear most frequently across all deals"
    )
    best_close_techniques: List[str] = Field(
        default_factory=list, description="Close techniques with highest observed win rates"
    )
    winning_patterns: List[str] = Field(
        default_factory=list, description="Behaviors / deal traits that consistently preceded wins"
    )
    losing_patterns: List[str] = Field(
        default_factory=list,
        description="Behaviors / deal traits that consistently preceded losses",
    )
    avg_deal_size_won_usd: Optional[float] = None
    avg_sales_cycle_days: Optional[float] = None
    actionable_recommendations: List[str] = Field(
        default_factory=list,
        description="Specific, prioritized advice to improve the current pipeline",
    )
    generated_at: str = Field(default="", description="ISO-8601 UTC timestamp of last refresh")
    insights_version: int = Field(default=0, description="Increments on each refresh")


# ---------------------------------------------------------------------------
# Outcome ingestion request models (used by API)
# ---------------------------------------------------------------------------


class RecordStageOutcomeRequest(BaseModel):
    """API payload to record the outcome of a single pipeline stage."""

    company_name: str
    stage: PipelineStage
    outcome: OutcomeResult
    pipeline_job_id: Optional[str] = None
    industry: Optional[str] = None
    icp_match_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    qualification_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    email_touch_number: Optional[int] = None
    subject_line_used: Optional[str] = None
    objection_text: Optional[str] = None
    close_technique_used: Optional[CloseType] = None
    notes: str = Field(default="")


class RecordDealOutcomeRequest(BaseModel):
    """API payload to record a final deal outcome (won or lost)."""

    company_name: str
    result: DealResult
    final_stage_reached: PipelineStage
    pipeline_job_id: Optional[str] = None
    industry: Optional[str] = None
    deal_size_usd: Optional[float] = Field(default=None, gt=0)
    loss_reason: Optional[str] = None
    win_factor: Optional[str] = None
    close_technique_used: Optional[CloseType] = None
    objections_raised: List[str] = Field(default_factory=list)
    stages_completed: List[PipelineStage] = Field(default_factory=list)
    icp_match_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    qualification_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sales_cycle_days: Optional[int] = None
    notes: str = Field(default="")


class SalesPipelineResult(BaseModel):
    """Full output of a sales pod pipeline run."""

    job_id: str
    entry_stage: PipelineStage
    product_name: str
    prospects: List[Prospect] = Field(default_factory=list)
    outreach_sequences: List[OutreachSequence] = Field(default_factory=list)
    qualified_leads: List[QualificationScore] = Field(default_factory=list)
    nurture_sequences: List[NurtureSequence] = Field(default_factory=list)
    discovery_plans: List[DiscoveryPlan] = Field(default_factory=list)
    proposals: List[SalesProposal] = Field(default_factory=list)
    closing_strategies: List[ClosingStrategy] = Field(default_factory=list)
    coaching_report: Optional[PipelineCoachingReport] = None
    summary: str = Field(default="", description="Plain-English summary of the full pipeline run")
