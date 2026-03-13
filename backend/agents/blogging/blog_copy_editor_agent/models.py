"""
Models for the blog copy editor agent (feedback on draft based on style guide).
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class FeedbackItem(BaseModel):
    """A single piece of copy editing feedback."""

    category: str = Field(
        ...,
        description="Category of feedback: voice, style, clarity, structure, technical, or formatting.",
    )
    severity: str = Field(
        ...,
        description="Severity: must_fix (violates style guide), should_fix (improves quality), consider (optional).",
    )
    location: Optional[str] = Field(
        None,
        description="Where in the draft the issue appears (e.g. 'paragraph 3', 'heading', 'opening hook').",
    )
    issue: str = Field(
        ...,
        description="Detailed description of the issue: what is wrong, which rule or principle it violates, and why it matters.",
    )
    suggestion: Optional[str] = Field(
        None,
        description="Concrete revision: how to change the text so the writer knows what to do and why it fixes the issue.",
    )


class CopyEditorInput(BaseModel):
    """Input for the blog copy editor agent."""

    draft: str = Field(
        ...,
        description="The blog post draft to review.",
    )
    style_guide: Optional[str] = Field(
        None,
        description="Full brand and writing style guide. If omitted, a default style checklist is used.",
    )
    audience: Optional[str] = Field(
        None,
        description="Intended audience (for context when evaluating tone and clarity).",
    )
    tone_or_purpose: Optional[str] = Field(
        None,
        description="Desired tone or purpose (for context).",
    )
    human_feedback: Optional[str] = Field(
        None,
        description="Author's explicit feedback or requested changes (e.g. from rejection). Incorporate into review.",
    )
    brand_spec_path: Optional[str] = Field(
        None,
        description="Path to brand_spec.yaml. When set, structured rules are used for evaluation.",
    )
    brand_spec: Optional[dict] = Field(
        None,
        description="Pre-loaded brand spec as dict. When set, used instead of brand_spec_path.",
    )
    previous_feedback_items: Optional[List[FeedbackItem]] = Field(
        None,
        description="Feedback items from the previous iteration, so the editor knows what was already addressed.",
    )


class CopyEditorOutput(BaseModel):
    """Output from the blog copy editor agent."""

    approved: bool = Field(
        default=False,
        description="True when the draft has no must_fix or should_fix issues and is ready to move forward.",
    )
    summary: str = Field(
        ...,
        description="Short note to the writer: overall context or priority (e.g. focus areas), not a summary of findings.",
    )
    feedback_items: List[FeedbackItem] = Field(
        default_factory=list,
        description="Detailed feedback items: each explains what is wrong, why it matters, and how to fix it.",
    )
