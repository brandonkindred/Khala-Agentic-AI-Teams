"""Request and response models for the social media marketing team API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from social_media_marketing_team.models import PostPerformanceObservation, TeamOutput
from social_media_marketing_team.trend_models import TrendDigest


class RunMarketingTeamRequest(BaseModel):
    brand_guidelines_path: str = Field(
        ..., max_length=4096, description="Path to brand guidelines document"
    )
    brand_objectives_path: str = Field(
        ..., max_length=4096, description="Path to brand objectives document"
    )
    llm_model_name: str = Field(..., max_length=256, description="Name of local LLM model to use")
    brand_name: str = Field(default="Brand", max_length=256)
    target_audience: str = Field(default="general audience", max_length=5000)
    goals: List[str] = Field(default_factory=lambda: ["engagement", "follower growth"])
    voice_and_tone: str = Field(default="professional, clear, and human", max_length=5000)
    cadence_posts_per_day: int = Field(default=2, ge=1, le=24)
    duration_days: int = Field(default=14, ge=1, le=365)
    human_approved_for_testing: bool = Field(default=False)
    human_feedback: str = Field(default="", max_length=50_000)


class ReviseMarketingTeamRequest(BaseModel):
    feedback: str = Field(..., min_length=3)
    approved_for_testing: bool = Field(default=False)


class RunMarketingTeamResponse(BaseModel):
    job_id: str
    status: str
    message: str


class MarketingJobStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    llm_model_name: str
    brand_guidelines_path: str
    brand_objectives_path: str
    last_updated_at: str
    eta_hint: Optional[str] = None
    error: Optional[str] = None
    result: Optional[TeamOutput] = None


class MarketingJobListItem(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    created_at: Optional[str] = None
    last_updated_at: Optional[str] = None


class PerformanceIngestRequest(BaseModel):
    observations: List[PostPerformanceObservation] = Field(default_factory=list)


class PerformanceIngestResponse(BaseModel):
    job_id: str
    campaign_name: Optional[str] = None
    observations_ingested: int
    message: str


class CancelMarketingJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteMarketingJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


class TrendRunResponse(BaseModel):
    message: str


class TrendLatestResponse(BaseModel):
    digest: TrendDigest
