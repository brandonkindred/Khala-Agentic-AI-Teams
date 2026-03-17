"""Models for the branding strategy multi-agent team.

Implements a 5-phase brand development framework:
  Phase 1 — Strategic Core
  Phase 2 — Narrative & Messaging
  Phase 3 — Visual & Expressive Identity
  Phase 4 — Experience & Channel Activation
  Phase 5 — Governance & Evolution
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared / legacy models
# ---------------------------------------------------------------------------


class Client(BaseModel):
    """Agency client; one client owns many brands."""

    id: str
    name: str = Field(..., min_length=1)
    created_at: str = ""
    updated_at: str = ""
    contact_info: Optional[str] = None
    notes: Optional[str] = None


class BrandStatus(str, Enum):
    draft = "draft"
    active = "active"
    evolving = "evolving"
    archived = "archived"


class WorkflowStatus(str, Enum):
    NEEDS_HUMAN_DECISION = "needs_human_decision"
    READY_FOR_ROLLOUT = "ready_for_rollout"


class BrandPhase(str, Enum):
    """Which phase the brand is currently in."""

    STRATEGIC_CORE = "strategic_core"
    NARRATIVE_MESSAGING = "narrative_messaging"
    VISUAL_IDENTITY = "visual_identity"
    CHANNEL_ACTIVATION = "channel_activation"
    GOVERNANCE = "governance"
    COMPLETE = "complete"


class PhaseGateStatus(str, Enum):
    """Gate status for phase transitions."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REVISION_REQUESTED = "revision_requested"


class ColorPalette(BaseModel):
    """A candidate color palette for brand identity selection."""

    name: str = ""
    description: str = ""
    colors: List[str] = Field(default_factory=list)
    sentiment: str = ""  # e.g. "warm and energetic", "cool and professional"


class BrandPhase(str, Enum):
    """Which phase the brand is currently in."""

    STRATEGIC_CORE = "strategic_core"
    NARRATIVE_MESSAGING = "narrative_messaging"
    VISUAL_IDENTITY = "visual_identity"
    CHANNEL_ACTIVATION = "channel_activation"
    GOVERNANCE = "governance"
    COMPLETE = "complete"


class PhaseGateStatus(str, Enum):
    """Gate status for phase transitions."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REVISION_REQUESTED = "revision_requested"


class BrandingMission(BaseModel):
    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    values: List[str] = Field(default_factory=list)
    differentiators: List[str] = Field(default_factory=list)
    desired_voice: str = "clear, confident, human"
    existing_brand_material: List[str] = Field(default_factory=list)
    wiki_path: Optional[str] = None
    # Visual identity fields — populated during guided palette selection
    color_inspiration: List[str] = Field(default_factory=list)
    color_palettes: List[ColorPalette] = Field(default_factory=list)
    selected_palette_index: Optional[int] = None
    visual_style: str = ""  # e.g. "minimalist", "maximalist", "editorial"
    typography_preference: str = ""  # e.g. "geometric sans-serif", "humanist serif"
    interface_density: str = ""  # e.g. "spacious/minimalist", "dense/information-rich"


class HumanReview(BaseModel):
    approved: bool = False
    feedback: str = ""


# ---------------------------------------------------------------------------
# Phase 1 — Strategic Core
# ---------------------------------------------------------------------------


class CoreValue(BaseModel):
    """A brand value with behavioral definition."""

    value: str
    behavioral_definition: str = ""
    observable_behaviors: List[str] = Field(default_factory=list)


class AudienceSegment(BaseModel):
    """A target audience segment with psychographic detail."""

    name: str
    description: str = ""
    pain_points: List[str] = Field(default_factory=list)
    goals: List[str] = Field(default_factory=list)
    decision_drivers: List[str] = Field(default_factory=list)


class DifferentiationPillar(BaseModel):
    """Competitive differentiation pillar with proof points."""

    pillar: str
    proof_points: List[str] = Field(default_factory=list)
    competitive_context: str = ""


class BrandDiscoveryAudit(BaseModel):
    """Brand discovery and audit findings."""

    current_brand_perception: str = ""
    market_position: str = ""
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    opportunities: List[str] = Field(default_factory=list)
    threats: List[str] = Field(default_factory=list)
    stakeholder_insights: List[str] = Field(default_factory=list)


class StrategicCoreOutput(BaseModel):
    """Phase 1 output: the strategic foundation everything else derives from."""

    brand_discovery: BrandDiscoveryAudit = Field(default_factory=BrandDiscoveryAudit)
    brand_purpose: str = ""
    mission_statement: str = ""
    vision_statement: str = ""
    core_values: List[CoreValue] = Field(default_factory=list)
    brand_promise: str = ""
    positioning_statement: str = ""
    target_audience_segments: List[AudienceSegment] = Field(default_factory=list)
    differentiation_pillars: List[DifferentiationPillar] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 2 — Narrative & Messaging
# ---------------------------------------------------------------------------


class BrandArchetype(BaseModel):
    """Brand archetype selection with rationale."""

    archetype: str
    rationale: str = ""
    personality_traits: List[str] = Field(default_factory=list)


class MessagingPillar(BaseModel):
    """A messaging pillar with proof points."""

    pillar: str
    key_message: str = ""
    proof_points: List[str] = Field(default_factory=list)


class AudienceMessageMap(BaseModel):
    """Message map tailored to a specific audience segment."""

    audience_segment: str
    primary_message: str = ""
    supporting_messages: List[str] = Field(default_factory=list)
    tone_adjustments: str = ""


class ElevatorPitch(BaseModel):
    """Tiered elevator pitch."""

    tier: str = ""  # e.g., "5-second", "30-second", "2-minute"
    pitch: str = ""


class PersonaProfile(BaseModel):
    """Rich persona profile with psychographic depth."""

    name: str
    role: str = ""
    demographics: str = ""
    psychographics: str = ""
    goals: List[str] = Field(default_factory=list)
    frustrations: List[str] = Field(default_factory=list)
    media_habits: List[str] = Field(default_factory=list)
    jobs_to_be_done: List[str] = Field(default_factory=list)


class NarrativeMessagingOutput(BaseModel):
    """Phase 2 output: the verbal identity of the brand."""

    brand_story: str = ""
    hero_narrative: str = ""
    brand_archetypes: List[BrandArchetype] = Field(default_factory=list)
    tagline: str = ""
    tagline_rationale: str = ""
    messaging_framework: List[MessagingPillar] = Field(default_factory=list)
    audience_message_maps: List[AudienceMessageMap] = Field(default_factory=list)
    elevator_pitches: List[ElevatorPitch] = Field(default_factory=list)
    boilerplate_variants: List[str] = Field(default_factory=list)
    persona_profiles: List[PersonaProfile] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 3 — Visual & Expressive Identity
# ---------------------------------------------------------------------------


class ColorEntry(BaseModel):
    """Color palette entry with rationale."""

    name: str
    hex_value: str = ""
    usage: str = ""
    psychological_rationale: str = ""


class TypographySpec(BaseModel):
    """Typography system specification."""

    role: str = ""  # e.g., "display", "body", "caption"
    font_family: str = ""
    weight_range: str = ""
    usage_notes: str = ""


class LogoUsageRule(BaseModel):
    """Logo suite and usage rules."""

    variant: str = ""  # e.g., "primary", "monochrome", "icon-only"
    usage_context: str = ""
    minimum_size: str = ""
    clear_space: str = ""


class VoiceToneEntry(BaseModel):
    """Voice and tone spectrum entry."""

    context: str = ""  # e.g., "marketing", "support", "legal"
    tone: str = ""
    examples: List[str] = Field(default_factory=list)


class VisualIdentityOutput(BaseModel):
    """Phase 3 output: the full design system and voice guide."""

    logo_suite: List[LogoUsageRule] = Field(default_factory=list)
    color_palette: List[ColorEntry] = Field(default_factory=list)
    typography_system: List[TypographySpec] = Field(default_factory=list)
    iconography_style: str = ""
    illustration_style: str = ""
    photography_direction: str = ""
    video_direction: str = ""
    motion_principles: List[str] = Field(default_factory=list)
    data_visualization_style: str = ""
    digital_adaptations: List[str] = Field(default_factory=list)
    voice_tone_spectrum: List[VoiceToneEntry] = Field(default_factory=list)
    language_dos: List[str] = Field(default_factory=list)
    language_donts: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 4 — Experience & Channel Activation
# ---------------------------------------------------------------------------


class ChannelGuideline(BaseModel):
    """Guidelines for a specific channel."""

    channel: str = ""  # e.g., "web", "social", "email", "events"
    strategy: str = ""
    dos: List[str] = Field(default_factory=list)
    donts: List[str] = Field(default_factory=list)
    content_types: List[str] = Field(default_factory=list)
    frequency_guidance: str = ""


class BrandArchitectureRule(BaseModel):
    """Brand architecture rules for multi-product organizations."""

    entity: str = ""  # e.g., "parent brand", "sub-brand", "product line"
    relationship: str = ""
    naming_convention: str = ""
    visual_treatment: str = ""


class BrandInActionExample(BaseModel):
    """Applied mockup or do/don't example."""

    context: str = ""
    correct_example: str = ""
    incorrect_example: str = ""
    rationale: str = ""


class ChannelActivationOutput(BaseModel):
    """Phase 4 output: activation playbook for marketing execution."""

    brand_experience_principles: List[str] = Field(default_factory=list)
    signature_moments: List[str] = Field(default_factory=list)
    sensory_elements: List[str] = Field(default_factory=list)
    channel_guidelines: List[ChannelGuideline] = Field(default_factory=list)
    brand_architecture: List[BrandArchitectureRule] = Field(default_factory=list)
    naming_conventions: List[str] = Field(default_factory=list)
    terminology_glossary: Dict[str, str] = Field(default_factory=dict)
    brand_in_action: List[BrandInActionExample] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 5 — Governance & Evolution
# ---------------------------------------------------------------------------


class ApprovalWorkflow(BaseModel):
    """Approval workflow definition."""

    asset_type: str = ""
    approvers: List[str] = Field(default_factory=list)
    sla: str = ""
    escalation_path: str = ""


class BrandHealthKPI(BaseModel):
    """Brand health tracking metric."""

    metric: str = ""
    measurement_method: str = ""
    target: str = ""
    review_frequency: str = ""


class GovernanceOutput(BaseModel):
    """Phase 5 output: operational layer for sustaining the brand."""

    ownership_model: str = ""
    decision_authority: Dict[str, str] = Field(default_factory=dict)
    approval_workflows: List[ApprovalWorkflow] = Field(default_factory=list)
    agency_briefing_protocols: List[str] = Field(default_factory=list)
    asset_management_guidance: List[str] = Field(default_factory=list)
    training_onboarding_plan: List[str] = Field(default_factory=list)
    brand_health_kpis: List[BrandHealthKPI] = Field(default_factory=list)
    tracking_methodology: str = ""
    review_trigger_points: List[str] = Field(default_factory=list)
    evolution_framework: str = ""
    version_control_cadence: str = ""


# ---------------------------------------------------------------------------
# Phase gate tracking
# ---------------------------------------------------------------------------


class PhaseGate(BaseModel):
    """Tracks the approval state of a phase transition."""

    phase: BrandPhase
    status: PhaseGateStatus = PhaseGateStatus.NOT_STARTED
    reviewer_feedback: str = ""


# ---------------------------------------------------------------------------
# Composite output
# ---------------------------------------------------------------------------


class BrandCheckRequest(BaseModel):
    asset_name: str
    asset_description: str


class BrandCheckResult(BaseModel):
    asset_name: str
    is_on_brand: bool
    confidence: float = Field(ge=0, le=1)
    rationale: List[str] = Field(default_factory=list)
    revision_suggestions: List[str] = Field(default_factory=list)


class CompetitiveSnapshot(BaseModel):
    """Market research result: competitive and similar brands context."""

    summary: str = ""
    similar_brands: List[str] = Field(default_factory=list)
    insights: List[str] = Field(default_factory=list)
    source: str = "market_research_team"


class DesignAssetRequestResult(BaseModel):
    """Result of a design asset request (stub or from StudioGrid)."""

    request_id: str
    status: str = "pending"
    artifacts: List[str] = Field(default_factory=list)


class BrandBook(BaseModel):
    """Consolidated brand document for handoff."""

    content: str = ""
    sections: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Legacy-compatible flat models (still used by compliance, wiki, etc.)
# ---------------------------------------------------------------------------


class BrandCodification(BaseModel):
    positioning_statement: str
    brand_promise: str
    brand_personality_traits: List[str] = Field(default_factory=list)
    narrative_pillars: List[str] = Field(default_factory=list)


class MoodBoardConcept(BaseModel):
    title: str
    visual_direction: str
    color_story: List[str] = Field(default_factory=list)
    typography_direction: str
    image_style: List[str] = Field(default_factory=list)


class CreativeRefinementPlan(BaseModel):
    phases: List[str] = Field(default_factory=list)
    workshop_prompts: List[str] = Field(default_factory=list)
    decision_criteria: List[str] = Field(default_factory=list)


class WritingGuidelines(BaseModel):
    voice_principles: List[str] = Field(default_factory=list)
    style_dos: List[str] = Field(default_factory=list)
    style_donts: List[str] = Field(default_factory=list)
    editorial_quality_bar: List[str] = Field(default_factory=list)


class DesignSystemDefinition(BaseModel):
    design_principles: List[str] = Field(default_factory=list)
    foundation_tokens: List[str] = Field(default_factory=list)
    component_standards: List[str] = Field(default_factory=list)


class WikiEntry(BaseModel):
    title: str
    summary: str
    owners: List[str] = Field(default_factory=list)
    update_cadence: str = "monthly"


# ---------------------------------------------------------------------------
# Team output — now includes phased outputs alongside legacy fields
# ---------------------------------------------------------------------------


class TeamOutput(BaseModel):
    status: WorkflowStatus
    mission_summary: str
    current_phase: BrandPhase = BrandPhase.STRATEGIC_CORE
    phase_gates: List[PhaseGate] = Field(default_factory=list)

    # Phase outputs
    strategic_core: Optional[StrategicCoreOutput] = None
    narrative_messaging: Optional[NarrativeMessagingOutput] = None
    visual_identity: Optional[VisualIdentityOutput] = None
    channel_activation: Optional[ChannelActivationOutput] = None
    governance: Optional[GovernanceOutput] = None

    # Legacy fields (populated from phase outputs for backward compatibility)
    codification: BrandCodification = Field(
        default_factory=lambda: BrandCodification(positioning_statement="", brand_promise="")
    )
    mood_boards: List[MoodBoardConcept] = Field(default_factory=list)
    creative_refinement: CreativeRefinementPlan = Field(default_factory=CreativeRefinementPlan)
    writing_guidelines: WritingGuidelines = Field(default_factory=WritingGuidelines)
    brand_guidelines: List[str] = Field(default_factory=list)
    design_system: DesignSystemDefinition = Field(default_factory=DesignSystemDefinition)
    wiki_backlog: List[WikiEntry] = Field(default_factory=list)
    brand_checks: List[BrandCheckResult] = Field(default_factory=list)
    human_feedback: Optional[str] = None
    competitive_snapshot: Optional[CompetitiveSnapshot] = None
    design_asset_result: Optional[DesignAssetRequestResult] = None
    brand_book: Optional[BrandBook] = None


# ---------------------------------------------------------------------------
# Brand version + top-level Brand model
# ---------------------------------------------------------------------------


class BrandVersionSummary(BaseModel):
    """Summary of a single brand run version for history."""

    version: int
    created_at: str
    status: Optional[str] = None


class Brand(BaseModel):
    """A brand owned by a client; can be evolved over time."""

    id: str
    client_id: str
    name: str = Field(..., min_length=1)
    status: BrandStatus = BrandStatus.draft
    current_phase: BrandPhase = BrandPhase.STRATEGIC_CORE
    mission: BrandingMission
    latest_output: Optional[TeamOutput] = None
    version: int = 0
    history: List[BrandVersionSummary] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
