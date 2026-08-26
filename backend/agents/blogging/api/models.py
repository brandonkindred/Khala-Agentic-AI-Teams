"""Pydantic request/response models for the blogging API, plus their small
model-building helpers (``_format_audience`` — a thin wrapper around the
shared ``agents.blogging.shared.audience.format_audience`` — and
``_blog_job_dict_to_status_response``).

Nothing here is monkeypatched by the test suite directly, so routers import these
names at plain module top level; ``api.main`` re-exports all of them because a few
tests instantiate models directly via ``_api_main.AudienceDetails(...)`` etc.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from agents.blogging.shared.audience import format_audience
from agents.blogging.shared.content_profile import ContentProfile, SeriesContext
from pydantic import BaseModel, Field


class AudienceDetails(BaseModel):
    """Audience details for targeting the content."""

    skill_level: Optional[str] = Field(
        None,
        description="e.g. 'beginner', 'intermediate', 'expert'.",
    )
    profession: Optional[str] = Field(
        None,
        description="e.g. 'CTO', 'developer', 'data scientist'.",
    )
    hobbies: Optional[List[str]] = Field(
        None,
        description="Relevant hobbies or interests.",
    )
    other: Optional[str] = Field(
        None,
        description="Any other audience context.",
    )


class TitleChoiceResponse(BaseModel):
    """A title choice with probability of success."""

    title: str
    probability_of_success: float


def _format_audience(audience: Optional[Union[AudienceDetails, str]]) -> str:
    """Convert audience input to a string for the agents."""
    return format_audience(audience) or ""


class FullPipelineRequest(BaseModel):
    """Request body for the full pipeline endpoint."""

    brief: str = Field(
        ..., max_length=50_000, description="Short description of the content topic."
    )
    title_concept: Optional[str] = Field(None, description="Optional idea or angle for the title.")
    audience: Optional[Union[AudienceDetails, str]] = Field(None, description="Audience details.")
    tone_or_purpose: Optional[str] = Field(
        None, description="e.g. 'educational', 'technical deep-dive'."
    )
    max_results: int = Field(20, ge=1, le=50, description="Maximum references.")
    run_gates: bool = Field(True, description="Run validators, fact-check, and compliance gates.")
    max_rewrite_iterations: int = Field(
        3, ge=1, le=10, description="Max rewrite iterations on FAIL."
    )
    content_profile: Optional[ContentProfile] = Field(
        None,
        description=(
            "Writing format (listicle, standard article, deep dive, series instalment). "
            "Drives length guidance when target_word_count is omitted; default is standard (~1000 words)."
        ),
    )
    series_context: Optional[SeriesContext] = Field(
        None,
        description="When this post is part of a series — scopes outline and draft to this instalment.",
    )
    length_notes: Optional[str] = Field(
        None,
        max_length=4000,
        description="Optional author notes merged into length/format guidance.",
    )
    target_word_count: Optional[int] = Field(
        None,
        ge=100,
        le=10000,
        description=(
            "Numeric word target override. When set, this wins for target length; soft bands scale from it. "
            "When omitted, length comes from content_profile (default standard_article ~1000)."
        ),
    )


class FullPipelineResponse(BaseModel):
    """Response from the full pipeline endpoint."""

    status: str = Field(..., description="PASS, FAIL, or NEEDS_HUMAN_REVIEW.")
    work_dir: str = Field(..., description="Path to artifact directory.")
    title_choices: List[TitleChoiceResponse] = Field(default_factory=list)
    outline: str = ""
    draft_preview: Optional[str] = Field(None, description="First 2000 chars of draft.")
    content_plan_summary: Optional[str] = Field(
        None,
        description="Short summary from the approved ContentPlan (topic + narrative flow).",
    )


class BlogJobStatusResponse(BaseModel):
    """Response for job status polling."""

    job_id: str
    status: str = Field(
        ..., description="pending, running, completed, failed, or needs_human_review"
    )
    phase: Optional[str] = Field(None, description="Current phase of the pipeline")
    progress: int = Field(0, ge=0, le=100, description="Overall progress 0-100")
    status_text: Optional[str] = Field(None, description="Human-readable status message")
    error: Optional[str] = Field(None, description="Error message if failed")
    failed_phase: Optional[str] = Field(None, description="Phase where failure occurred")
    title_choices: List[TitleChoiceResponse] = Field(default_factory=list)
    outline: Optional[str] = Field(None, description="Blog outline if available")
    draft_preview: Optional[str] = Field(None, description="First 2000 chars of draft")
    work_dir: Optional[str] = Field(None, description="Path to artifact directory")
    research_sources_count: int = Field(0, description="Number of research sources found")
    draft_iterations: int = Field(0, description="Number of draft iterations completed")
    rewrite_iterations: int = Field(0, description="Number of rewrite iterations completed")
    created_at: Optional[str] = Field(None, description="Job creation timestamp")
    started_at: Optional[str] = Field(None, description="Job start timestamp")
    completed_at: Optional[str] = Field(None, description="Job completion timestamp")
    approved_at: Optional[str] = Field(
        None, description="When the job was approved (ISO timestamp)"
    )
    approved_by: Optional[str] = Field(None, description="Who approved the job (optional)")
    job_type: Optional[str] = Field(None, description="Job category, e.g. medium_stats")
    content_plan_summary: Optional[str] = Field(
        None,
        description="Short summary from ContentPlan when pipeline completed planning",
    )
    content_plan_detail: Optional[str] = Field(
        None,
        description="Full content plan as human-readable markdown (titles, outline, requirements analysis)",
    )
    planning_iterations_used: Optional[int] = Field(
        None, description="Planning refine iterations completed"
    )
    parse_retry_count: Optional[int] = Field(
        None, description="JSON parse/repair attempts during planning"
    )
    planning_wall_ms_total: Optional[float] = Field(
        None, description="Wall-clock ms spent in planning phase"
    )
    planning_failure_reason: Optional[str] = Field(
        None,
        description="When status is failed and failed_phase is planning, machine-readable reason",
    )
    # Title selection collaboration fields
    waiting_for_title_selection: bool = Field(
        False,
        description="True when the pipeline is paused waiting for the author to select a title",
    )
    selected_title: Optional[str] = Field(
        None, description="Title chosen by the author from the planning candidates"
    )
    # Story elicitation collaboration fields
    waiting_for_story_input: bool = Field(
        False,
        description="True when the ghost writer agent is waiting for the author's story response",
    )
    story_gaps: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Story gap opportunities identified by the ghost writer agent",
    )
    current_story_gap_index: int = Field(
        0, description="Index of the story gap currently being elicited"
    )
    current_gap_round: int = Field(
        0, description="Round counter for story gap iteration — frontend filters chat by this"
    )
    story_chat_history: List[Dict[str, Any]] = Field(
        default_factory=list, description="Multi-turn conversation between ghost writer and author"
    )
    elicited_stories: List[str] = Field(
        default_factory=list,
        description="Compiled first-person story narratives from the interview",
    )
    # General Q&A collaboration fields
    pending_questions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Questions from pipeline agents waiting for author answers",
    )
    waiting_for_answers: bool = Field(
        False, description="True when the pipeline is paused waiting for Q&A answers"
    )
    # Interactive draft review collaboration fields
    waiting_for_draft_feedback: bool = Field(
        False,
        description="True when the pipeline is paused waiting for the editor to review a draft",
    )
    draft_for_review: Optional[str] = Field(
        None,
        description="Full draft text currently awaiting editor review",
    )
    draft_review_revision: int = Field(
        0,
        description="Which revision number is currently being reviewed",
    )
    draft_review_questions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Uncertainty questions the writer agent wants the editor to answer",
    )
    draft_escalation_summary: Optional[str] = Field(
        None,
        description="Summary of why the copy-edit loop is stuck (present when escalating after 10+ revisions)",
    )
    guideline_updates_applied: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Writing guideline updates derived from editor feedback during this job",
    )


def _blog_job_dict_to_status_response(
    job: Dict[str, Any], job_id_fallback: str
) -> BlogJobStatusResponse:
    """Map persisted job dict to API response (single place for optional planning fields)."""
    title_choices: List[TitleChoiceResponse] = []
    for tc in job.get("title_choices", []):
        if isinstance(tc, dict):
            title_choices.append(
                TitleChoiceResponse(
                    title=tc.get("title", ""),
                    probability_of_success=tc.get("probability_of_success", 0.0),
                )
            )
    return BlogJobStatusResponse(
        job_id=job.get("job_id", job_id_fallback),
        status=job.get("status", "pending"),
        phase=job.get("phase"),
        progress=job.get("progress", 0),
        status_text=job.get("status_text"),
        error=job.get("error"),
        failed_phase=job.get("failed_phase"),
        title_choices=title_choices,
        outline=job.get("outline"),
        draft_preview=job.get("draft_preview"),
        work_dir=job.get("work_dir"),
        research_sources_count=job.get("research_sources_count", 0),
        draft_iterations=job.get("draft_iterations", 0),
        rewrite_iterations=job.get("rewrite_iterations", 0),
        created_at=job.get("created_at"),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        approved_at=job.get("approved_at"),
        approved_by=job.get("approved_by"),
        job_type=job.get("job_type"),
        content_plan_summary=job.get("content_plan_summary"),
        content_plan_detail=job.get("content_plan_detail"),
        planning_iterations_used=job.get("planning_iterations_used"),
        parse_retry_count=job.get("parse_retry_count"),
        planning_wall_ms_total=job.get("planning_wall_ms_total"),
        planning_failure_reason=job.get("planning_failure_reason"),
        waiting_for_title_selection=bool(job.get("waiting_for_title_selection", False)),
        selected_title=job.get("selected_title"),
        waiting_for_story_input=bool(job.get("waiting_for_story_input", False)),
        story_gaps=job.get("story_gaps", []),
        current_story_gap_index=job.get("current_story_gap_index", 0),
        current_gap_round=job.get("current_gap_round", 0),
        story_chat_history=job.get("story_chat_history", []),
        elicited_stories=job.get("elicited_stories", []),
        pending_questions=job.get("pending_questions", []),
        waiting_for_answers=bool(job.get("waiting_for_answers", False)),
        waiting_for_draft_feedback=bool(job.get("waiting_for_draft_feedback", False)),
        draft_for_review=job.get("draft_for_review"),
        draft_review_revision=job.get("draft_review_revision", 0),
        draft_review_questions=job.get("draft_review_questions", []),
        draft_escalation_summary=job.get("draft_escalation_summary"),
        guideline_updates_applied=job.get("guideline_updates_applied", []),
    )


class BlogJobListItem(BaseModel):
    """Summary item for job listing."""

    job_id: str
    status: str
    brief: str = Field(..., description="First 100 chars of the brief")
    phase: Optional[str] = None
    progress: int = 0
    created_at: Optional[str] = None
    job_type: Optional[str] = None


class ArtifactMeta(BaseModel):
    """Metadata for a single artifact (name and optional producer phase/agent)."""

    name: str = Field(..., description="Artifact filename")
    producer_phase: Optional[str] = Field(
        None, description="Pipeline phase that produced this artifact"
    )
    producer_agent: Optional[str] = Field(
        None, description="Agent or component that produced this artifact"
    )


class ArtifactListResponse(BaseModel):
    """Response listing artifact names that exist for a job, with optional producer metadata."""

    artifacts: List[ArtifactMeta] = Field(
        ..., description="Existing artifacts with name and producer metadata"
    )


class ArtifactContentResponse(BaseModel):
    """Response with the content of a single artifact (string for .md/.yaml, object for .json)."""

    name: str = Field(..., description="Artifact filename")
    content: Union[str, Dict[str, Any], List[Any]] = Field(
        ..., description="Artifact content as string or parsed JSON (dict/list)"
    )


class StartPipelineResponse(BaseModel):
    """Response from starting an async pipeline."""

    job_id: str
    message: str = "Pipeline started"


class CancelJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


class SelectTitleRequest(BaseModel):
    """Request body for title selection."""

    title: str = Field(..., description="The author-chosen title from the planning candidates.")


class TitleRatingItem(BaseModel):
    """A single title rating."""

    title: str
    rating: str = Field(..., description="One of: dislike, like, love")


class RateTitlesRequest(BaseModel):
    """Request body for title ratings."""

    ratings: List[TitleRatingItem]


class StoryResponseRequest(BaseModel):
    """Request body for a story elicitation response."""

    message: str = Field(..., description="The author's response to the ghost writer's question.")


class BlogAnswersRequest(BaseModel):
    """Request body for submitting Q&A answers."""

    answers: List[Dict[str, Any]] = Field(
        ...,
        description="List of answer objects (question_id, selected_option_id, selected_answer, etc.).",
    )


class DraftFeedbackRequest(BaseModel):
    """Request body for submitting feedback on a draft during interactive review."""

    feedback: str = Field(
        default="",
        description="Free-form feedback text from the editor about the draft.",
    )
    approved: bool = Field(
        default=False,
        description="True if the editor approves the draft as-is (no further revisions needed).",
    )
