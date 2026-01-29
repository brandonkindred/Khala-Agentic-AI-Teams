from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Literal, Dict, Any

from pydantic import BaseModel, HttpUrl, Field


class ResearchBriefInput(BaseModel):
    """Top-level input to the research agent."""

    brief: str = Field(..., description="Short free-text description of the content need.")
    audience: Optional[str] = Field(
        None, description="Intended audience for the eventual content (e.g. 'CTOs', 'beginners')."
    )
    tone_or_purpose: Optional[str] = Field(
        None,
        description="High-level purpose or tone, e.g. 'educational', 'persuasive', 'technical deep-dive'.",
    )
    max_results: int = Field(
        10,
        ge=1,
        le=50,
        description="Total number of references to return.",
    )
    per_query_limit: int = Field(
        8,
        ge=1,
        le=20,
        description="Maximum number of search results to consider per generated query.",
    )
    recency_preference: Optional[Literal["latest_12_months", "no_preference"]] = Field(
        "latest_12_months",
        description="Preference for document recency.",
    )


class SearchQuery(BaseModel):
    """Represents a single search query derived from the brief."""

    query_text: str
    intent: Optional[str] = Field(
        None,
        description="High-level intent label for this query, e.g. 'overview', 'how-to', 'stats', 'risks'.",
    )


class CandidateResult(BaseModel):
    """Raw search result before fetching full content."""

    title: str
    url: HttpUrl
    snippet: Optional[str] = None
    source: Optional[str] = Field(
        None,
        description="Search provider or tool identifier.",
    )
    rank: Optional[int] = Field(
        None,
        description="Rank within the search provider results (1-based).",
    )
    score_components: Dict[str, float] = Field(
        default_factory=dict,
        description="Optional raw scoring components (e.g. relevance, authority, recency).",
    )


class SourceDocument(BaseModel):
    """Fetched and lightly processed web document."""

    url: HttpUrl
    title: Optional[str] = None
    content: str
    publish_date: Optional[datetime] = None
    domain: Optional[str] = None
    language: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ResearchReference(BaseModel):
    """Normalized reference returned by the agent."""

    title: str
    url: HttpUrl
    domain: Optional[str] = None
    summary: str
    key_points: List[str] = Field(default_factory=list)
    type: Optional[str] = Field(
        None,
        description='High-level type label, e.g. "guides", "academic", "news", "tooling".',
    )
    recency: Optional[str] = Field(
        None,
        description="Human-readable recency or publication date (e.g. '2024-05', 'within last year').",
    )
    relevance_score: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Composite relevance score between 0 and 1.",
    )


class ResearchAgentOutput(BaseModel):
    """Top-level structured result from the agent."""

    query_plan: List[SearchQuery]
    references: List[ResearchReference]
    notes: Optional[str] = Field(
        None,
        description="Optional high-level synthesis, caveats, and suggestions.",
    )

