"""
Models for the blog draft agent (draft from research document + outline).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DraftInput(BaseModel):
    """Input for the blog draft agent: research document, outline, and optional style guide."""

    research_document: str = Field(
        ...,
        description="Compiled research document (sources, summaries, key points) to base the draft on.",
    )
    outline: str = Field(
        ...,
        description="Blog post outline with section headings and notes for the first draft.",
    )
    audience: Optional[str] = Field(
        None,
        description="Intended audience (e.g. 'beginners', 'CTOs').",
    )
    tone_or_purpose: Optional[str] = Field(
        None,
        description="Desired tone or purpose, e.g. 'educational', 'technical deep-dive'.",
    )
    style_guide: Optional[str] = Field(
        None,
        description="Full brand and writing style guide text. If omitted, a minimal style reminder is used.",
    )


class DraftOutput(BaseModel):
    """Output from the blog draft agent: the blog post draft in Markdown."""

    draft: str = Field(
        ...,
        description="Full blog post draft in Markdown, compliant with the provided style guide.",
    )
