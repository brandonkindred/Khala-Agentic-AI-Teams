"""
Models for the blog writer agent (write from content plan).
"""

from __future__ import annotations

from typing import List, Optional

from blog_copy_editor_agent.models import FeedbackItem
from pydantic import BaseModel, Field, model_validator
from shared.content_plan import ContentPlan, content_plan_to_outline_markdown

from .feedback_tracker import PersistentFeedbackItem

# ---------------------------------------------------------------------------
# Interactive draft review models
# ---------------------------------------------------------------------------


class WritingGuidelineUpdate(BaseModel):
    """A single update to apply to the writing guidelines based on user feedback."""

    category: str = Field(
        ...,
        description="Category of the update: tone, cadence, structure, vocabulary, patterns, voice, or other.",
    )
    description: str = Field(
        ...,
        description="Human-readable description of the guideline change.",
    )
    guideline_text: str = Field(
        ...,
        description="The new guideline rule or modification to append/merge into the writing style guide.",
    )


class UserDraftFeedback(BaseModel):
    """User/editor feedback on a draft during the interactive review cycle."""

    approved: bool = Field(
        default=False,
        description="True if the user approves the draft as-is (no further revisions needed).",
    )
    feedback: Optional[str] = Field(
        None,
        description="Free-form feedback text from the user about the draft.",
    )
    guideline_updates_requested: bool = Field(
        default=False,
        description=(
            "Set to True when the user's feedback references tone, cadence, sound, "
            "writing patterns, or content structure, indicating the writing guidelines "
            "should be updated."
        ),
    )


class UncertaintyQuestion(BaseModel):
    """A question the writer agent needs answered before proceeding with confidence."""

    question_id: str = Field(..., description="Unique identifier for this question.")
    question: str = Field(..., description="The question text for the user.")
    context: str = Field(
        ...,
        description="Why the agent is uncertain and how the answer will affect the draft.",
    )
    section: Optional[str] = Field(
        None,
        description="Which section of the draft this uncertainty relates to.",
    )


class DraftReviewResult(BaseModel):
    """Result of the writer agent's analysis after producing a draft, before user review."""

    draft: str = Field(..., description="The draft text.")
    uncertainty_questions: List[UncertaintyQuestion] = Field(
        default_factory=list,
        description="Questions the agent wants to ask the user before finalizing.",
    )
    revision_number: int = Field(
        default=1,
        description="Which revision of the draft this is (1 = initial).",
    )


class WriterInput(BaseModel):
    """Input for the blog writer agent: approved content plan and writing context."""

    content_plan: ContentPlan = Field(
        ...,
        description="Approved structured plan (sections, narrative flow, titles).",
    )
    audience: Optional[str] = Field(
        None,
        description="Intended audience (e.g. 'beginners', 'CTOs').",
    )
    tone_or_purpose: Optional[str] = Field(
        None,
        description="Desired tone or purpose, e.g. 'educational', 'technical deep-dive'.",
    )
    target_word_count: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Target word count for the draft. Writer will aim for approximately this length.",
    )
    length_guidance: str = Field(
        default="",
        description="Qualitative length/format instructions (content profile). Appended to target length in prompts.",
    )
    selected_title: Optional[str] = Field(
        None,
        description="Title chosen by the author from the planning candidates. When set, the draft MUST use this exact title as the H1 heading.",
    )
    elicited_stories: Optional[str] = Field(
        None,
        description=(
            "First-person story narratives elicited by the ghost writer agent. "
            "Incorporate these into the relevant sections to personalise the post."
        ),
    )

    def outline_for_prompt(self) -> str:
        """Flattened outline + narrative for LLM prompts (replaces legacy outline-only string)."""
        return content_plan_to_outline_markdown(self.content_plan)

    @model_validator(mode="after")
    def _validate_plan_required(self) -> "WriterInput":
        if not self.content_plan:
            raise ValueError("WriterInput requires a content_plan")
        return self


class WriterOutput(BaseModel):
    """Output from the blog writer agent: the blog post draft in Markdown."""

    draft: str = Field(
        ...,
        description="Full blog post draft in Markdown, compliant with the provided style guide.",
    )


class RevisionPlanChange(BaseModel):
    """A single planned change in the revision plan."""

    section: str = Field(..., description="Which section or location in the draft this change targets.")
    feedback_ids: List[int] = Field(
        default_factory=list, description="1-based indices of feedback items addressed by this change."
    )
    action: str = Field(..., description="What will be done: rewrite, delete, merge, add, rephrase, etc.")
    rationale: str = Field(..., description="Why this change is needed and what it fixes.")


class RevisionPlan(BaseModel):
    """Structured plan produced before executing a draft revision.

    Persisted as ``revision_plan_{iteration}.json`` in the job's work directory
    so the user can see exactly what the agent intends to do.
    """

    summary: str = Field(..., description="One-paragraph overview of the revision strategy.")
    changes: List[RevisionPlanChange] = Field(
        default_factory=list, description="Ordered list of planned changes (priority order)."
    )
    risks: List[str] = Field(
        default_factory=list,
        description="Potential regressions or trade-offs the plan is aware of.",
    )


class ReviseWriterInput(BaseModel):
    """Input for revising a draft based on copy editor or compliance feedback."""

    draft: str = Field(..., description="The current draft to revise.")
    feedback_items: List[FeedbackItem] = Field(
        ...,
        description="Copy editor feedback to apply when revising.",
    )
    feedback_summary: Optional[str] = Field(
        None,
        description="Overall copy editor summary (for context).",
    )
    previous_feedback_items: Optional[List[FeedbackItem]] = Field(
        None,
        description="Feedback from the prior iteration, so the writer knows what was already addressed.",
    )
    persistent_issues: Optional[List[PersistentFeedbackItem]] = Field(
        None,
        description="Issues flagged multiple times across iterations with occurrence counts and suggestions.",
    )
    content_plan: ContentPlan = Field(
        ...,
        description="Original content plan — preserve structure and section intent when revising.",
    )
    audience: Optional[str] = Field(None, description="Intended audience.")
    tone_or_purpose: Optional[str] = Field(None, description="Desired tone or purpose.")
    target_word_count: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Target word count for the revised draft.",
    )
    length_guidance: str = Field(
        default="",
        description="Qualitative length/format instructions; same as initial draft when revising.",
    )
    selected_title: Optional[str] = Field(
        None,
        description="Author-chosen title; preserve this exact H1 when revising.",
    )
    elicited_stories: Optional[str] = Field(
        None,
        description="First-person story narratives elicited by the ghost writer agent; preserve in revision.",
    )

    def outline_for_prompt(self) -> str:
        return content_plan_to_outline_markdown(self.content_plan)
