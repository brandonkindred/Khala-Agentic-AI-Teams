"""Models for the branding strategy multi-agent team."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class WorkflowStatus(str, Enum):
    NEEDS_HUMAN_DECISION = "needs_human_decision"
    READY_FOR_ROLLOUT = "ready_for_rollout"


class BrandingMission(BaseModel):
    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    values: List[str] = Field(default_factory=list)
    differentiators: List[str] = Field(default_factory=list)
    desired_voice: str = "clear, confident, human"
    existing_brand_material: List[str] = Field(default_factory=list)
    wiki_path: Optional[str] = None


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
