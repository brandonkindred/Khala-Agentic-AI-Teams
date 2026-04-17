"""Request and response models for the social media marketing team API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from social_media_marketing_team.models import PostPerformanceObservation, TeamOutput
from social_media_marketing_team.trend_models import TrendDigest

# Only allow alphanumeric, hyphens, and underscores in identifiers.
# Prevents path-traversal (../) and query-string injection (?/#) when
# the values are interpolated into URLs or error messages.
_SAFE_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"


class RunMarketingTeamRequest(BaseModel):
    client_id: str = Field(
        ...,
        max_length=256,
        pattern=_SAFE_ID_PATTERN,
        description="Client identifier from the branding team",
    )
    brand_id: str = Field(
        ...,
        max_length=256,
        pattern=_SAFE_ID_PATTERN,
        description="Brand identifier from the branding team",
    )
    llm_model_name: str = Field(..., max_length=256, description="Name of local LLM model to use")
    goals: List[str] = Field(default_factory=lambda: ["engagement", "follower growth"])
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
    brand_summary: Optional[str] = None


class MarketingJobStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str
    progress: int
    llm_model_name: str
    client_id: str
    brand_id: str
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


class WinningPostCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    body: str = Field(default="", max_length=50_000)
    platform: str = Field(default="", max_length=64)
    keywords: List[str] = Field(default_factory=list)
    metrics: Dict[str, float] = Field(default_factory=dict)
    engagement_score: float = Field(default=0.0, ge=0.0, le=1.0)
    linked_goals: List[str] = Field(default_factory=list)
    source_job_id: Optional[str] = Field(default=None, max_length=256)
    summary: Optional[str] = Field(default=None, max_length=2_000)


class WinningPostResponse(BaseModel):
    id: str
    title: str
    body: str
    platform: str
    keywords: List[str]
    metrics: Dict[str, Any]
    engagement_score: float
    linked_goals: List[str]
    summary: str
    source_job_id: Optional[str] = None
    created_at: str


class WinningPostCreateResponse(BaseModel):
    id: str
    message: str = "Winning post saved."


class WinningPostDeleteResponse(BaseModel):
    id: str
    message: str = "Winning post deleted."
