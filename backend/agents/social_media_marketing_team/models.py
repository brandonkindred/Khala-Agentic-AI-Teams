"""Models for the social media marketing multi-agent team."""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class Platform(str, Enum):
    LINKEDIN = "linkedin"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    X = "x"


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_REVISION = "needs_revision"
    APPROVED_FOR_TESTING = "approved_for_testing"


class BrandGoals(BaseModel):
    brand_name: str
    target_audience: str
    goals: List[str] = Field(default_factory=list)
    voice_and_tone: str = "professional, clear, and human"
    cadence_posts_per_day: int = Field(default=2, ge=1, le=24)
    duration_days: int = Field(default=14, ge=1, le=365)
    brand_guidelines: str = ""
    brand_objectives: str = ""
    messaging_pillars: List[str] = Field(default_factory=list)
    brand_story: str = ""
    tagline: str = ""


class CampaignProposal(BaseModel):
    campaign_name: str
    objective: str
    audience_hypothesis: str
    messaging_pillars: List[str] = Field(default_factory=list)
    channel_mix_strategy: Dict[Platform, str] = Field(default_factory=dict)
    success_metrics: List[str] = Field(default_factory=list)
    experiment_notes: str = ""
    consensus_score: float = 0.0
    communication_log: List[str] = Field(default_factory=list)


class HumanReview(BaseModel):
    approved: bool
    feedback: str = ""


class ConceptIdea(BaseModel):
    title: str
    concept: str
    target_platforms: List[Platform]
    linked_goals: List[str] = Field(default_factory=list)
    primary_hook: str = ""
    suggested_visual: str = ""
    content_format: str = ""  # e.g. carousel, reel, thread, long-form, story
    cta_variant: str = ""
    brand_fit_score: float = Field(ge=0, le=1)
    audience_resonance_score: float = Field(ge=0, le=1)
    goal_alignment_score: float = Field(ge=0, le=1)
    estimated_engagement_probability: float = Field(ge=0, le=1)
    risk_level: str = "low"
    risk_reasons: List[str] = Field(default_factory=list)
    # Reference exemplars from the Winning Posts Bank. Stored on a
    # separate field — never inlined into ``concept`` — so risk and
    # routing scanners only see the agent's own copy.
    prior_winners_context: str = ""


class ContentPlan(BaseModel):
    campaign_name: str
    cadence_posts_per_day: int
    duration_days: int
    total_required_posts: int
    approved_ideas: List[ConceptIdea] = Field(default_factory=list)


class PlatformExecutionPlan(BaseModel):
    platform: Platform
    posting_guidelines: List[str] = Field(default_factory=list)
    first_week_schedule: List[str] = Field(default_factory=list)
    kpi_focus: List[str] = Field(default_factory=list)


class ExperimentArm(BaseModel):
    name: str
    arm_type: str  # control or variant
    hypothesis: str
    success_criteria: List[str] = Field(default_factory=list)


class ExperimentPlan(BaseModel):
    campaign_name: str
    minimum_runtime_days: int = 7
    minimum_sample_size_per_arm: int = 500
    arms: List[ExperimentArm] = Field(default_factory=list)


class MetricDefinition(BaseModel):
    name: str
    value: float = Field(ge=0)


class PostPerformanceObservation(BaseModel):
    campaign_name: str
    platform: Platform
    concept_title: str
    posted_at: str
    metrics: List[MetricDefinition] = Field(default_factory=list)


class CampaignPerformanceSnapshot(BaseModel):
    campaign_name: str
    observations: List[PostPerformanceObservation] = Field(default_factory=list)


class TeamOutput(BaseModel):
    status: CampaignStatus
    proposal: CampaignProposal
    human_feedback: Optional[str] = None
    content_plan: Optional[ContentPlan] = None
    platform_execution_plans: List[PlatformExecutionPlan] = Field(default_factory=list)
    llm_model_name: str = ""
    experiment_plan: Optional[ExperimentPlan] = None
    ingested_performance: List[PostPerformanceObservation] = Field(default_factory=list)
    winners_retrieved: int = 0
