"""
Models for the blog draft agent (draft from research document + content plan).
"""

from __future__ import annotations

from typing import List, Optional

from blog_copy_editor_agent.models import FeedbackItem
from blog_research_agent.models import ResearchReference
from pydantic import BaseModel, Field, model_validator
from shared.content_plan import ContentPlan, content_plan_to_outline_markdown


class DraftInput(BaseModel):
    """Input for the blog draft agent: research and approved content plan."""

    research_document: Optional[str] = Field(
        None,
        description="Compiled research document (fallback when research_references not used).",
    )
    research_references: Optional[List[ResearchReference]] = Field(
        None,
        description="Individual research sources; when non-empty, agent extracts notes/citations per source in parallel then drafts from combined notes.",
    )
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
    allowed_claims: Optional[dict] = Field(
        None,
        description="Pre-loaded allowed_claims.json. When set, writer must use only these claims and tag as [CLAIM:id].",
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
    def require_research_source(self) -> "DraftInput":
        has_doc = self.research_document and self.research_document.strip()
        has_refs = self.research_references and len(self.research_references) > 0
        if not has_doc and not has_refs:
            raise ValueError(
                "DraftInput requires either research_document or non-empty research_references"
            )
        return self


class DraftOutput(BaseModel):
    """Output from the blog draft agent: the blog post draft in Markdown."""

    draft: str = Field(
        ...,
        description="Full blog post draft in Markdown, compliant with the provided style guide.",
    )


class ReviseDraftInput(BaseModel):
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
    research_document: Optional[str] = Field(
        None,
        description="Original research document (for context when revising).",
    )
    content_plan: ContentPlan = Field(
        ...,
        description="Original content plan — preserve structure and section intent when revising.",
    )
    audience: Optional[str] = Field(None, description="Intended audience.")
    tone_or_purpose: Optional[str] = Field(None, description="Desired tone or purpose.")
    allowed_claims: Optional[dict] = Field(
        None,
        description="Pre-loaded allowed_claims.json. When set, preserve claim tags [CLAIM:id] and do not add new factual claims.",
    )
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
