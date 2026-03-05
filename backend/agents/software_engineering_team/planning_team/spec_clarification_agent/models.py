"""Models for the Spec Clarification Agent."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class SpecClarificationInput(BaseModel):
    """Input for the Spec Clarification Agent."""

    spec_content: str = Field(..., description="Raw spec content")
    prior_turns: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Chat history: list of {role, message}",
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description="Open questions needing clarification",
    )
    assumptions: List[str] = Field(
        default_factory=list,
        description="Current assumptions",
    )
    last_question_asked: str | None = Field(
        None,
        description="The question we asked that the user is now answering",
    )
    user_message: str | None = Field(
        None,
        description="User's answer to the last question (when processing an answer)",
    )


class SpecClarificationOutput(BaseModel):
    """Output from the Spec Clarification Agent."""

    assistant_message: str = Field(..., description="Next question or completion message")
    open_questions: List[str] = Field(
        default_factory=list,
        description="Remaining open questions after this turn",
    )
    assumptions: List[str] = Field(
        default_factory=list,
        description="Updated assumptions",
    )
    resolved_questions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Accumulated resolved questions: {question, answer, category}",
    )
    confidence_score: float = Field(
        default=0.0,
        description="Confidence that spec is clear enough (0-1)",
    )
    done_clarifying: bool = Field(
        default=False,
        description="True when no more questions or confidence threshold reached",
    )
