"""Models for the branding strategy multi-agent team."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class ColorPalette(BaseModel):
    """A candidate color palette for brand identity selection."""

    name: str = ""
    description: str = ""
    colors: List[str] = Field(default_factory=list)
    sentiment: str = ""  # e.g. "warm and energetic", "cool and professional"


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
    status: str = "pending"  # pending | completed
    artifacts: List[str] = Field(default_factory=list)


class BrandBook(BaseModel):
    """Consolidated brand document for handoff."""

    content: str = ""  # markdown or structured text
    sections: Dict[str, Any] = Field(default_factory=dict)  # optional structured fields


class TeamOutput(BaseModel):
    status: WorkflowStatus
    mission_summary: str
    codification: BrandCodification
    mood_boards: List[MoodBoardConcept] = Field(default_factory=list)
    creative_refinement: CreativeRefinementPlan
    writing_guidelines: WritingGuidelines
    brand_guidelines: List[str] = Field(default_factory=list)
    design_system: DesignSystemDefinition
    wiki_backlog: List[WikiEntry] = Field(default_factory=list)
    brand_checks: List[BrandCheckResult] = Field(default_factory=list)
    human_feedback: Optional[str] = None
    competitive_snapshot: Optional[CompetitiveSnapshot] = None
    design_asset_result: Optional[DesignAssetRequestResult] = None
    brand_book: Optional[BrandBook] = None


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
    mission: BrandingMission
    latest_output: Optional[TeamOutput] = None
    version: int = 0
    history: List[BrandVersionSummary] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
