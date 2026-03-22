"""Models for the ghost writer story elicitation agent."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class StoryGap(BaseModel):
    """A section in the content plan where a personal story would strengthen the post."""

    section_title: str = Field(..., description="Title of the content plan section")
    section_context: str = Field(
        ...,
        description="What this section is about and the argument it makes",
    )
    seed_question: str = Field(
        ...,
        description="Opening question the ghost writer asks Brandon to surface a personal story",
    )


class StoryElicitationResult(BaseModel):
    """Result of eliciting a personal story for one story gap."""

    gap: StoryGap
    narrative: Optional[str] = Field(
        None,
        description=(
            "Compiled first-person narrative ready to hand to the draft agent. "
            "None if the user skipped or provided insufficient detail."
        ),
    )
    skipped: bool = Field(False, description="True if the user explicitly skipped this gap")
    rounds_used: int = Field(0, description="Number of conversation turns used")
