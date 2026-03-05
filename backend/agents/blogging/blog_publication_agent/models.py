"""
Models for the blog publication agent (submit draft, approval, rejection, platform export).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


class SubmitDraftInput(BaseModel):
    """Input for submitting a final draft for publication."""

    draft: str = Field(..., description="The final blog post draft in Markdown.")
    title: Optional[str] = Field(
        None,
        description="Post title. If omitted, derived from first H1 in draft.",
    )
    slug: Optional[str] = Field(
        None,
        description="URL-safe slug for the post folder. If omitted, derived from title.",
    )
    audience: Optional[str] = Field(None, description="Intended audience (for context).")
    tone_or_purpose: Optional[str] = Field(None, description="Tone or purpose (for context).")
    tags: Optional[List[str]] = Field(
        None,
        description="Tags for dev.to and other platforms (e.g. ['terraform', 'aws']).",
    )


class PublicationSubmission(BaseModel):
    """Result of submitting a draft for publication."""

    submission_id: str = Field(..., description="Unique identifier for this submission.")
    slug: str = Field(..., description="URL-safe slug used for the post.")
    file_path: Path = Field(..., description="Path to the draft markdown file.")
    state: str = Field(
        "awaiting_approval",
        description="Current state: awaiting_approval, collecting_rejection_feedback, approved.",
    )
    message: str = Field(
        "Draft written. Awaiting your approval. Call approve() when ready, or reject() with feedback.",
        description="Human-readable status message.",
    )


class ApprovalResult(BaseModel):
    """Result of approving a submission."""

    submission_id: str = Field(..., description="The approved submission id.")
    folder_path: Path = Field(..., description="Path to the post folder.")
    draft_path: Path = Field(..., description="Path to draft.md.")
    medium_path: Path = Field(..., description="Path to medium.md.")
    devto_path: Path = Field(..., description="Path to devto.md.")
    substack_path: Path = Field(..., description="Path to substack.md.")
    message: str = Field(..., description="Success message.")


class RejectionResponse(BaseModel):
    """Response from rejecting a submission (may include follow-up questions)."""

    submission_id: str = Field(..., description="The submission id.")
    questions: List[str] = Field(
        default_factory=list,
        description="Follow-up questions to gather more details from the human.",
    )
    ready_to_revise: bool = Field(
        False,
        description="True when enough feedback collected; call run_revision_loop() next.",
    )
    collected_feedback_summary: Optional[str] = Field(
        None,
        description="Summary of all collected feedback when ready_to_revise.",
    )


class RevisionLoopResult(BaseModel):
    """Result of running the draft-editor revision loop after rejection."""

    submission_id: str = Field(..., description="The submission id.")
    revised_draft: str = Field(..., description="The revised draft after applying feedback.")
    iterations_completed: int = Field(..., description="Number of draft-editor iterations run.")
    message: str = Field(
        ...,
        description="Draft updated. State reset to awaiting_approval. Review and approve or reject again.",
    )


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    import re

    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug[:80].rstrip("-") or "untitled"


class PublishingPack(BaseModel):
    """SEO and packaging output for publish-ready content."""

    title_options: List[str] = Field(
        default_factory=list,
        description="Alternative title options for A/B testing.",
    )
    meta_description: Optional[str] = Field(
        None,
        description="Meta description for SEO (155 chars or less).",
    )
    header_polish: Optional[str] = Field(
        None,
        description="Polished H1/H2 suggestions.",
    )
    internal_links: List[str] = Field(
        default_factory=list,
        description="Suggested internal links.",
    )
    snippet_copy: Optional[str] = Field(
        None,
        description="Social snippet or preview copy.",
    )
    tags: List[str] = Field(default_factory=list, description="Suggested tags.")


class PublicationMetadata(BaseModel):
    """Persisted metadata for a publication submission."""

    submission_id: str
    slug: str
    title: str
    draft_content: str
    audience: Optional[str] = None
    tone_or_purpose: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    state: str = "awaiting_approval"
    rejection_feedback: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    approved_at: Optional[str] = None

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> "PublicationMetadata":
        return cls.model_validate_json(path.read_text())
