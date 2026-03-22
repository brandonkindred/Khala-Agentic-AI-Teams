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
        description="Category of feedback: e.g. voice, style, clarity, structure, flow, engagement, technical, formatting, authenticity, length.",
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
    previous_feedback_items: Optional[List[FeedbackItem]] = Field(
        None,
        description="Feedback items from the previous iteration, so the editor knows what was already addressed.",
    )
    target_word_count: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description=(
            "Nominal target word count. Programmatic over-length flags compare ratio to this value; "
            "when soft_max_words is set, drafts at or below that ceiling are not flagged merely for exceeding target."
        ),
    )
    length_guidance: str = Field(
        default="",
        description="Content profile / qualitative length expectations for evaluation context.",
    )
    soft_min_words: Optional[int] = Field(
        None,
        description="Lower soft band; optional under-length hint for deep profiles.",
    )
    soft_max_words: Optional[int] = Field(
        None,
        description=(
            "Upper soft band. When set, drafts with word count at or below this value do not receive "
            "programmatic over-length (structure) feedback; above it, must_fix/should_fix use ratios vs target_word_count."
        ),
    )
    editor_must_fix_over_ratio: float = Field(
        default=1.3,
        ge=1.0,
        le=3.0,
        description="actual/target above this ratio triggers must_fix length feedback.",
    )
    editor_should_fix_over_ratio: float = Field(
        default=1.1,
        ge=1.0,
        le=2.0,
        description="actual/target above this ratio triggers should_fix length feedback.",
    )
    content_profile: Optional[str] = Field(
        None,
        description="Resolved profile id (e.g. technical_deep_dive) for under-length hints.",
    )
    content_plan_context: Optional[str] = Field(
        None,
        description="Approved content plan (narrative + sections). Editor should align feedback with this structure.",
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
