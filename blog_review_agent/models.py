"""
Models for the blog review agent (title choices and outline from brief + sources).
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

# ResearchReference is produced by the research agent; review agent consumes it.
from blog_research_agent.models import ResearchReference


class TitleChoice(BaseModel):
    """A candidate title/soundbite with estimated probability of reaching a large audience."""

    title: str = Field(..., description="Catchy title or soundbite aimed at conversion.")
    probability_of_success: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Estimated probability (0–1) of success at reaching a large audience.",
    )


class BlogReviewInput(BaseModel):
    """Input for the blog review agent: original brief context and researched sources."""

    brief: str = Field(..., description="Original content brief or topic.")
    audience: Optional[str] = Field(
        None,
        description="Intended audience (e.g. 'CTOs', 'beginners').",
    )
    tone_or_purpose: Optional[str] = Field(
        None,
        description="Desired tone or purpose, e.g. 'educational', 'technical deep-dive'.",
    )
    references: List[ResearchReference] = Field(
        ...,
        description="List of researched sources (summaries and key points) to use for titles and outline.",
    )


class BlogReviewOutput(BaseModel):
    """Output from the blog review agent: title choices and blog outline."""

    title_choices: List[TitleChoice] = Field(
        ...,
        description="Top 5 high-quality title choices with probability of success.",
    )
    outline: str = Field(
        ...,
        description="Detailed blog post outline with notes and details useful for a first draft.",
    )
