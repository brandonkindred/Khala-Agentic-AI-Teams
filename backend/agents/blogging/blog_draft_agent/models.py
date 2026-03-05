"""
Models for the blog draft agent (draft from research document + outline).
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from blog_copy_editor_agent.models import FeedbackItem


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
    brand_spec_path: Optional[str] = Field(
        None,
        description="Path to brand_spec.yaml. When set, structured rules are injected into the prompt.",
    )
    brand_spec: Optional[dict] = Field(
        None,
        description="Pre-loaded brand spec as dict. When set, used instead of brand_spec_path.",
    )
    allowed_claims: Optional[dict] = Field(
        None,
        description="Pre-loaded allowed_claims.json. When set, writer must use only these claims and tag as [CLAIM:id].",
    )


class DraftOutput(BaseModel):
    """Output from the blog draft agent: the blog post draft in Markdown."""

    draft: str = Field(
        ...,
        description="Full blog post draft in Markdown, compliant with the provided style guide.",
    )


class ReviseDraftInput(BaseModel):
    """Input for revising a draft based on copy editor feedback."""

    draft: str = Field(..., description="The current draft to revise.")
    feedback_items: List[FeedbackItem] = Field(
        ...,
        description="Copy editor feedback to apply when revising.",
    )
    feedback_summary: Optional[str] = Field(
        None,
        description="Overall copy editor summary (for context).",
    )
    research_document: Optional[str] = Field(
        None,
        description="Original research document (for context when revising).",
    )
    outline: Optional[str] = Field(
        None,
        description="Original outline (for context when revising).",
    )
    audience: Optional[str] = Field(None, description="Intended audience.")
    tone_or_purpose: Optional[str] = Field(None, description="Desired tone or purpose.")
    style_guide: Optional[str] = Field(None, description="Full brand and writing style guide.")
    brand_spec_path: Optional[str] = Field(None, description="Path to brand_spec.yaml.")
    brand_spec: Optional[dict] = Field(None, description="Pre-loaded brand spec as dict.")
    allowed_claims: Optional[dict] = Field(
        None,
        description="Pre-loaded allowed_claims.json. When set, preserve claim tags [CLAIM:id] and do not add new factual claims.",
    )
