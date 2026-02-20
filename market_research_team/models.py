"""Models for market research and business concept viability team workflows."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class TeamTopology(str, Enum):
    """How the orchestrator should structure the research workflow."""

    UNIFIED = "unified"
    SPLIT = "split"


class WorkflowStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_HUMAN_DECISION = "needs_human_decision"
    READY_FOR_EXECUTION = "ready_for_execution"


class ResearchMission(BaseModel):
    product_concept: str
    target_users: str
    business_goal: str
    topology: TeamTopology = TeamTopology.UNIFIED
    transcript_folder_path: Optional[str] = None
    transcripts: List[str] = Field(default_factory=list)


class HumanReview(BaseModel):
    approved: bool
    feedback: str = ""


class InterviewInsight(BaseModel):
    source: str
    user_jobs: List[str] = Field(default_factory=list)
    pain_points: List[str] = Field(default_factory=list)
    desired_outcomes: List[str] = Field(default_factory=list)
    direct_quotes: List[str] = Field(default_factory=list)


class MarketSignal(BaseModel):
    signal: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[str] = Field(default_factory=list)


class ViabilityRecommendation(BaseModel):
    verdict: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: List[str] = Field(default_factory=list)
    suggested_next_experiments: List[str] = Field(default_factory=list)


class TeamOutput(BaseModel):
    status: WorkflowStatus
    topology: TeamTopology
    mission_summary: str
    insights: List[InterviewInsight] = Field(default_factory=list)
    market_signals: List[MarketSignal] = Field(default_factory=list)
    recommendation: ViabilityRecommendation
    proposed_research_scripts: List[str] = Field(default_factory=list)
    human_feedback: Optional[str] = None
