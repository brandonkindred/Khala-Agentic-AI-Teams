"""Pydantic models for the AI Sales Team pipeline."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


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

    industry: List[str] = Field(default_factory=list, description="Target industries, e.g. ['SaaS', 'FinTech']")
    company_size_min: int = Field(default=10, description="Minimum employee count")
    company_size_max: int = Field(default=5000, description="Maximum employee count")
    job_titles: List[str] = Field(default_factory=list, description="Target buyer titles, e.g. ['VP Sales', 'CRO']")
    pain_points: List[str] = Field(default_factory=list, description="Core pains the product solves")
    budget_range_usd: str = Field(default="$10k–$100k/yr", description="Expected ACV range")
    geographic_focus: List[str] = Field(default_factory=list, description="Target regions or countries")
    tech_stack_keywords: List[str] = Field(
        default_factory=list, description="Technologies the prospect likely uses"
    )
    disqualifying_traits: List[str] = Field(
        default_factory=list, description="Traits that rule out a prospect"
    )


class Prospect(BaseModel):
    """Raw lead identified during prospecting — not yet contacted."""

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


# ---------------------------------------------------------------------------
# Outreach models
# ---------------------------------------------------------------------------


class EmailTouch(BaseModel):
    """A single email in a multi-touch cold outreach sequence."""

    day: int = Field(..., description="Day in the sequence to send this email (0-indexed)")
    subject_line: str
    body: str
    personalization_tokens: List[str] = Field(
        default_factory=list, description="Merge fields to fill before sending"
    )
    call_to_action: str = Field(default="", description="Specific ask at the end of this touch")


class OutreachSequence(BaseModel):
    """Complete multi-channel outreach plan for a prospect."""

    prospect: Prospect
    email_sequence: List[EmailTouch] = Field(default_factory=list)
    call_script: str = Field(default="", description="Cold-call opener and talk track")
    linkedin_message: str = Field(default="", description="Connection request / InMail copy")
    sequence_rationale: str = Field(
        default="", description="Why this angle was chosen for this prospect"
    )


# ---------------------------------------------------------------------------
# Qualification models
# ---------------------------------------------------------------------------


class BANTScore(BaseModel):
    """BANT qualification framework scores."""

    budget: int = Field(..., ge=0, le=10, description="Does the prospect have budget? 0–10")
    authority: int = Field(..., ge=0, le=10, description="Is the contact a decision-maker? 0–10")
    need: int = Field(..., ge=0, le=10, description="Is there a confirmed, urgent need? 0–10")
    timeline: int = Field(..., ge=0, le=10, description="Is there a defined purchase timeline? 0–10")


class MEDDICScore(BaseModel):
    """MEDDIC qualification framework signals."""

    metrics_identified: bool = Field(default=False, description="Quantified business impact defined")
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
    value_creation_level: int = Field(
        ..., ge=1, le=4, description="Iannarino Level 1–4 value tier"
    )
    recommended_action: str = Field(
        ..., description="Next step: advance, nurture, or disqualify"
    )
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
    success_criteria_for_call: str = Field(default="", description="What a successful call looks like")


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
    urgency_framing: str = Field(default="", description="Legitimate urgency lever to accelerate decision")
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
    velocity_insights: str = Field(default="", description="Pipeline velocity and stage duration analysis")
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
    max_prospects: int = Field(default=5, ge=1, le=20, description="Max leads to generate")
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
    max_prospects: int = Field(default=5, ge=1, le=20)
    company_context: str = Field(default="", max_length=5000)


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
