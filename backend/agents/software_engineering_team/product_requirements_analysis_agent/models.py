"""
Models for the Product Requirements Analysis Agent.

Enhanced question models with rationale and confidence for auto-answering.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class AnalysisPhase(str, Enum):
    """Phases of the Product Requirements Analysis workflow."""

    CONTEXT_DISCOVERY = "context_discovery"
    SPEC_REVIEW = "spec_review"
    COMMUNICATE = "communicate"
    SPEC_UPDATE = "spec_update"
    SPEC_CLEANUP = "spec_cleanup"


class QuestionOption(BaseModel):
    """A selectable option for an open question with rationale."""

    id: str = Field(description="Unique option identifier")
    label: str = Field(description="Display text for this option")
    is_default: bool = Field(
        default=False, description="Whether this is the recommended default"
    )
    rationale: str = Field(
        default="",
        description="Why this option is recommended based on industry best practices",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0-1.0) that this is the best choice",
    )


class OpenQuestion(BaseModel):
    """An open question with selectable options and enhanced metadata."""

    id: str = Field(description="Unique question identifier")
    question_text: str = Field(description="The question text")
    context: str = Field(
        default="", description="Additional context explaining why this matters"
    )
    recommendation: str = Field(
        default="",
        description="Short recommendation: which option to choose and why, considering alternatives.",
    )
    options: List[QuestionOption] = Field(
        default_factory=list, description="2-3 selectable options with rationale"
    )
    allow_multiple: bool = Field(
        default=False,
        description="Whether user can select multiple options for this question",
    )
    source: str = Field(
        default="spec_review", description="Origin of this question"
    )
    category: str = Field(
        default="general",
        description="Category: architecture, security, ux, performance, business, etc.",
    )
    priority: str = Field(
        default="medium",
        description="Priority: high, medium, low",
    )
    
    # Constraint drilling fields for systematic technology decision tracking
    constraint_domain: str = Field(
        default="",
        description="Constraint domain: infrastructure, frontend, backend, database, auth, or empty for non-constraint questions",
    )
    constraint_layer: int = Field(
        default=0,
        description="Layer depth (1-4) within the constraint domain. 0 = not a constraint question",
    )
    depends_on: Optional[str] = Field(
        default=None,
        description="Question ID this depends on (for follow-up questions in a drilling chain)",
    )
    blocking: bool = Field(
        default=True,
        description="Whether this question blocks final PRD completion",
    )
    owner: str = Field(
        default="user",
        description="Owner expected to answer (e.g. user, stakeholder, security_team)",
    )
    section_impact: List[str] = Field(
        default_factory=list,
        description="PRD sections impacted by this question",
    )
    due_date: str = Field(
        default="",
        description="Suggested due date (ISO date) for receiving an answer",
    )
    status: str = Field(
        default="open",
        description="Question lifecycle status: open, asked, answered, stale",
    )
    asked_via: List[str] = Field(
        default_factory=list,
        description="Delivery channels used for this question (slack, email, web_ui, etc.)",
    )


class AnsweredQuestion(BaseModel):
    """A question that has been answered (by user or auto-answer)."""

    question_id: str = Field(description="ID of the original question")
    question_text: str = Field(description="The question text")
    selected_option_id: str = Field(
        default="", description="ID of the selected option (for single-select)"
    )
    selected_option_ids: List[str] = Field(
        default_factory=list,
        description="IDs of selected options (for multi-select questions)",
    )
    selected_answer: str = Field(description="Text of the selected answer(s)")
    was_auto_answered: bool = Field(
        default=False, description="Whether auto-answer was used"
    )
    was_default: bool = Field(
        default=False, description="Whether the default was applied (fallback)"
    )
    rationale: str = Field(
        default="", description="Rationale for the answer (from auto-answer or user)"
    )
    confidence: float = Field(
        default=0.0, description="Confidence score if auto-answered"
    )
    other_text: str = Field(
        default="", description="Custom text if 'other' was selected"
    )


class AutoAnswerResult(BaseModel):
    """Result from auto-answering a question."""

    question_id: str = Field(description="ID of the question being answered")
    selected_option_id: str = Field(description="ID of the selected option")
    selected_answer: str = Field(description="Text of the selected answer")
    rationale: str = Field(
        description="Detailed explanation of why this is the best choice"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0-1.0) in this answer",
    )
    risks: List[str] = Field(
        default_factory=list,
        description="Potential risks of this choice",
    )
    alternatives_considered: str = Field(
        default="",
        description="Brief note on why other options were not selected",
    )
    industry_references: List[str] = Field(
        default_factory=list,
        description="Industry best practices or references supporting this choice",
    )


class SpecReviewResult(BaseModel):
    """Output of Spec Review phase."""

    issues: List[str] = Field(
        default_factory=list, description="Issues identified in the spec"
    )
    gaps: List[str] = Field(
        default_factory=list, description="Gaps or missing requirements"
    )
    open_questions: List[OpenQuestion] = Field(
        default_factory=list, description="Questions requiring clarification"
    )
    summary: str = Field(default="", description="Summary of the review")


class SpecCleanupResult(BaseModel):
    """Output of Spec Cleanup phase."""

    is_valid: bool = Field(
        default=False, description="Whether the spec passed validation"
    )
    validation_issues: List[str] = Field(
        default_factory=list, description="Issues found during validation"
    )
    cleaned_spec: str = Field(
        default="", description="The cleaned and validated spec content"
    )
    summary: str = Field(default="", description="Summary of cleanup actions")


class AnalysisWorkflowResult(BaseModel):
    """Full result of the Product Requirements Analysis workflow."""

    success: bool = Field(default=False)
    current_phase: Optional[AnalysisPhase] = None
    summary: str = Field(default="")
    failure_reason: str = Field(default="")
    spec_review_result: Optional[SpecReviewResult] = None
    spec_cleanup_result: Optional[SpecCleanupResult] = None
    answered_questions: List[AnsweredQuestion] = Field(default_factory=list)
    iterations: int = Field(
        default=0, description="Number of spec review iterations performed"
    )
    validated_spec_path: Optional[str] = Field(
        default=None, description="Path to the validated spec file"
    )
    final_spec_content: Optional[str] = Field(
        default=None, description="Final validated spec content"
    )
