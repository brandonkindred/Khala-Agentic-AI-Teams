"""Models for the social media trend discovery agent."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class TrendingTopic(BaseModel):
    """A single trending topic identified across social media platforms."""

    title: str = Field(description="Concise headline (5-15 words)")
    summary: str = Field(
        description="2-3 sentence description of the trend and why it is gaining traction"
    )
    platforms: List[str] = Field(
        default_factory=list, description="Platforms where this trend was observed"
    )
    sources: List[str] = Field(
        default_factory=list, description="Up to 3 source URLs from search results"
    )
    relevance_score: float = Field(ge=0.0, le=1.0, description="Estimated trend strength, 0.0–1.0")


class TrendDigest(BaseModel):
    """Daily digest of the top trending topics across social media."""

    generated_at: str = Field(description="ISO 8601 UTC timestamp when this digest was produced")
    topics: List[TrendingTopic] = Field(
        default_factory=list, description="Top trending topics (up to 3)"
    )
    platforms_searched: List[str] = Field(
        default_factory=list, description="Platforms included in the search"
    )
    search_query_count: int = Field(default=0, description="Number of web search queries executed")
