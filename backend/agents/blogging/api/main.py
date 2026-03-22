"""
FastAPI application exposing research + planning (research-and-review) and the full pipeline.

The review-only agent has been replaced by a planning phase that produces a ContentPlan.
Supports synchronous and asynchronous execution with job polling for UI integration.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

import json as json_module  # noqa: E402

from blog_medium_stats_agent.agent import BlogMediumStatsAgent  # noqa: E402
from blog_medium_stats_agent.models import MediumStatsReport, MediumStatsRunConfig  # noqa: E402
from blog_research_agent.models import ResearchBriefInput  # noqa: E402
from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from shared.brand_spec import brand_spec_prompt_configured  # noqa: E402
from shared.content_plan import (  # noqa: E402
    content_plan_summary_text,
    content_plan_to_outline_markdown,
)
from shared.content_profile import (  # noqa: E402
    ContentProfile,
    SeriesContext,
    resolve_length_policy,
)
from shared.errors import BloggingError, PlanningError  # noqa: E402
from shared.medium_integration_access import medium_stats_integration_eligible  # noqa: E402
from shared.medium_stats_api import MediumStatsRequest  # noqa: E402

from llm_service import OllamaLLMClient  # noqa: E402

try:
    from shared.artifacts import ARTIFACT_NAMES, ARTIFACT_PRODUCER, read_artifact, write_artifact
except ImportError:
    ARTIFACT_NAMES = ()
    ARTIFACT_PRODUCER = {}
    read_artifact = None
    write_artifact = None

try:
    from shared.blog_job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_NEEDS_REVIEW,
        approve_blog_job,
        complete_blog_job,
        create_blog_job,
        delete_blog_job,
        fail_blog_job,
        get_blog_job,
        list_blog_jobs,
        medium_stats_run_dir,
        skip_current_story_gap,
        start_blog_job,
        submit_blog_answers,
        submit_story_user_message,
        submit_title_selection,
        unapprove_blog_job,
        update_blog_job,
    )
except ImportError:
    create_blog_job = None
    delete_blog_job = None
    get_blog_job = None
    list_blog_jobs = None
    update_blog_job = None
    start_blog_job = None
    complete_blog_job = None
    fail_blog_job = None
    approve_blog_job = None
    unapprove_blog_job = None
    medium_stats_run_dir = None
    submit_title_selection = None
    submit_story_user_message = None
    skip_current_story_gap = None
    submit_blog_answers = None
    JOB_STATUS_COMPLETED = "completed"
    JOB_STATUS_NEEDS_REVIEW = "needs_human_review"
    BloggingError = Exception

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Base directory for run artifacts (when work_dir is requested)
RUN_ARTIFACTS_BASE = Path(tempfile.gettempdir()) / "blogging_runs"

app = FastAPI(
    title="Blog Research & Review API",
    description="Runs research and review agents. Supports sync and async execution with job polling for UI.",
    version="0.3.0",
)


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


class ResearchAndReviewRequest(BaseModel):
    """Request body for the research-and-review endpoint."""

    brief: str = Field(..., max_length=50_000, description="Short description of the content topic.")
    title_concept: Optional[str] = Field(
        None,
        description="Optional idea or angle for the title.",
    )
    audience: Optional[Union[AudienceDetails, str]] = Field(
        None,
        description="Audience details (object or free-text string).",
    )
    tone_or_purpose: Optional[str] = Field(
        None,
        description="e.g. 'educational', 'technical deep-dive', 'persuasive'.",
    )
    max_results: int = Field(
        20,
        ge=1,
        le=50,
        description="Maximum number of references to return.",
    )
    work_dir: Optional[str] = Field(
        None,
        description="Optional directory path for persisting research/outline artifacts.",
    )
    run_id: Optional[str] = Field(
        None,
        description="Optional run ID; artifacts are written to a subdir under RUN_ARTIFACTS_BASE when set.",
    )
    content_profile: Optional[ContentProfile] = Field(
        None,
        description="Writing format; drives planning length/scope (default standard article).",
    )
    series_context: Optional[SeriesContext] = Field(
        None,
        description="When this post is part of a series — scopes planning to this instalment.",
    )
    length_notes: Optional[str] = Field(
        None,
        max_length=4000,
        description="Optional author notes merged into length/format guidance for planning.",
    )
    target_word_count: Optional[int] = Field(
        None,
        ge=100,
        le=10000,
        description="Numeric word target override for planning/draft length.",
    )


class TitleChoiceResponse(BaseModel):
    """A title choice with probability of success."""

    title: str
    probability_of_success: float


class ResearchAndReviewResponse(BaseModel):
    """Response from the research-and-review endpoint."""

    title_choices: List[TitleChoiceResponse] = Field(
        ...,
        description="Top title choices with probability of success.",
    )
    outline: str = Field(
        ...,
        description="Detailed blog outline with notes for the first draft.",
    )
    compiled_document: Optional[str] = Field(
        None,
        description="Formatted research document (sources, academic papers, similar topics).",
    )
    notes: Optional[str] = Field(
        None,
        description="High-level synthesis and suggestions from the research agent.",
    )


def _format_audience(audience: Optional[Union[AudienceDetails, str]]) -> str:
    """Convert audience input to a string for the agents."""
    if audience is None:
        return ""
    if isinstance(audience, str):
        return audience.strip()
    parts = []
    if audience.skill_level:
        parts.append(f"skill level: {audience.skill_level}")
    if audience.profession:
        parts.append(f"profession: {audience.profession}")
    if audience.hobbies:
        parts.append(f"interests: {', '.join(audience.hobbies)}")
    if audience.other:
        parts.append(audience.other)
    return "; ".join(parts) if parts else ""


# Shared LLM client (initialized on first request or at startup)
_llm_client: Optional[OllamaLLMClient] = None


def _get_llm_client() -> OllamaLLMClient:
    """Get or create the shared LLM client."""
    global _llm_client
    if _llm_client is None:
        _llm_client = OllamaLLMClient()
    return _llm_client


@app.post(
    "/research-and-review",
    response_model=ResearchAndReviewResponse,
    summary="Run research and planning pipeline",
    description=(
        "Executes research (web + arXiv) then structured content planning: title choices and outline "
        "from the approved content plan."
    ),
)
def research_and_review(request: ResearchAndReviewRequest) -> ResearchAndReviewResponse:
    """
    Run research then planning (same planning step as the full pipeline).

    Returns title choices, outline derived from the content plan, and the compiled research document.
    """
    try:
        llm_client = _get_llm_client()
    except Exception as e:
        logger.exception("Failed to initialize LLM client")
        raise HTTPException(status_code=500, detail=f"Agent initialization failed: {e}") from e

    llm_requests_before = llm_client.request_count

    brief_text = request.brief.strip()
    if request.title_concept:
        brief_text = f"{brief_text}. Title concept: {request.title_concept.strip()}"

    audience_str = _format_audience(request.audience)

    work_dir = None
    if request.work_dir:
        work_dir = Path(request.work_dir)
    elif request.run_id:
        work_dir = RUN_ARTIFACTS_BASE / request.run_id
        work_dir.mkdir(parents=True, exist_ok=True)

    brief_input = ResearchBriefInput(
        brief=brief_text,
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
        max_results=request.max_results,
    )

    length_policy = resolve_length_policy(
        content_profile=request.content_profile,
        explicit_target_word_count=request.target_word_count,
        length_notes=request.length_notes,
        series_context=request.series_context,
    )

    try:
        from agent_implementations.blog_writing_process_v2 import run_research_and_planning

        research_result, _research_document, planning_phase_result = run_research_and_planning(
            brief_input,
            work_dir=work_dir,
            llm_client=llm_client,
            length_policy=length_policy,
            series_context=request.series_context,
            job_updater=None,
        )
    except PlanningError as e:
        logger.exception("Planning failed")
        raise HTTPException(
            status_code=422,
            detail={
                "error": "planning_failed",
                "message": str(e),
                "failure_reason": getattr(e, "failure_reason", None),
            },
        ) from e
    except Exception as e:
        logger.exception("Research-and-planning failed")
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}") from e

    plan = planning_phase_result.content_plan
    outline = content_plan_to_outline_markdown(plan)

    llm_requests_after = llm_client.request_count
    logger.info(
        "Completed research-and-planning pipeline with %s LLM requests",
        llm_requests_after - llm_requests_before,
    )

    return ResearchAndReviewResponse(
        title_choices=[
            TitleChoiceResponse(
                title=tc.title,
                probability_of_success=tc.probability_of_success,
            )
            for tc in plan.title_candidates
        ],
        outline=outline,
        compiled_document=research_result.compiled_document,
        notes=research_result.notes,
    )


def _run_research_review_with_tracking(job_id: str, request: ResearchAndReviewRequest) -> None:
    """Run research + planning in a background thread with job tracking."""
    try:
        import sys
        from pathlib import Path as P

        _root = P(__file__).resolve().parent.parent
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        from agent_implementations.blog_writing_process_v2 import run_research_and_planning

        llm_client = _get_llm_client()
    except Exception as e:
        logger.exception("Failed to initialize for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=f"Agent initialization failed: {e}")
        return

    brief_text = request.brief.strip()
    if request.title_concept:
        brief_text = f"{brief_text}. Title concept: {request.title_concept.strip()}"
    audience_str = _format_audience(request.audience)

    work_dir = RUN_ARTIFACTS_BASE / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    if update_blog_job is not None:
        try:
            update_blog_job(job_id, work_dir=str(work_dir))
        except Exception as e:
            logger.warning("Failed to set work_dir for job %s: %s", job_id, e)

    brief_input = ResearchBriefInput(
        brief=brief_text,
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
        max_results=request.max_results,
    )

    length_policy = resolve_length_policy(
        content_profile=request.content_profile,
        explicit_target_word_count=request.target_word_count,
        length_notes=request.length_notes,
        series_context=request.series_context,
    )

    if start_blog_job is not None:
        start_blog_job(job_id)

    def job_updater(**kwargs: Any) -> None:
        if update_blog_job is not None:
            try:
                update_blog_job(job_id, **kwargs)
            except Exception as e:
                logger.warning("Failed to update job %s: %s", job_id, e)

    try:
        research_result, _rd, planning_phase_result = run_research_and_planning(
            brief_input,
            work_dir=work_dir,
            llm_client=llm_client,
            length_policy=length_policy,
            series_context=request.series_context,
            job_updater=job_updater,
        )
    except PlanningError as e:
        logger.exception("Planning failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(
                job_id,
                error=str(e),
                failed_phase="planning",
                planning_failure_reason=getattr(e, "failure_reason", None),
            )
        return
    except Exception as e:
        logger.exception("Research-and-planning failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=f"Pipeline failed: {e}")
        return

    plan = planning_phase_result.content_plan
    outline = content_plan_to_outline_markdown(plan)
    title_choices = [
        {"title": tc.title, "probability_of_success": tc.probability_of_success}
        for tc in plan.title_candidates
    ]
    if complete_blog_job is not None:
        complete_blog_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            title_choices=title_choices,
            outline=outline,
        )
    logger.info("Completed research-and-planning job %s", job_id)


class FullPipelineRequest(BaseModel):
    """Request body for the full pipeline endpoint."""

    brief: str = Field(..., max_length=50_000, description="Short description of the content topic.")
    title_concept: Optional[str] = Field(None, description="Optional idea or angle for the title.")
    audience: Optional[Union[AudienceDetails, str]] = Field(None, description="Audience details.")
    tone_or_purpose: Optional[str] = Field(None, description="e.g. 'educational', 'technical deep-dive'.")
    max_results: int = Field(20, ge=1, le=50, description="Maximum references.")
    run_gates: bool = Field(True, description="Run validators, fact-check, and compliance gates.")
    max_rewrite_iterations: int = Field(3, ge=1, le=10, description="Max rewrite iterations on FAIL.")
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


@app.post(
    "/full-pipeline",
    response_model=FullPipelineResponse,
    summary="Run full blog pipeline with gates",
    description="Runs research -> planning -> draft -> validators -> compliance -> rewrite loop. Persists all artifacts.",
)
def full_pipeline(request: FullPipelineRequest) -> FullPipelineResponse:
    """Run the full brand-aligned pipeline with artifact persistence and gates."""
    import sys
    from pathlib import Path
    _blogging_root = Path(__file__).resolve().parent.parent
    if str(_blogging_root) not in sys.path:
        sys.path.insert(0, str(_blogging_root))
    from agent_implementations.blog_writing_process_v2 import run_pipeline

    run_id = str(uuid.uuid4())[:8]
    work_dir = RUN_ARTIFACTS_BASE / run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    brief_text = request.brief.strip()
    if request.title_concept:
        brief_text = f"{brief_text}. Title concept: {request.title_concept.strip()}"
    audience_str = _format_audience(request.audience)

    brief_input = ResearchBriefInput(
        brief=brief_text,
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
        max_results=request.max_results,
    )

    length_policy = resolve_length_policy(
        content_profile=request.content_profile,
        explicit_target_word_count=request.target_word_count,
        length_notes=request.length_notes,
        series_context=request.series_context,
    )
    try:
        research_result, planning_phase_result, draft_result, status = run_pipeline(
            brief_input,
            work_dir=work_dir,
            run_gates=request.run_gates,
            max_rewrite_iterations=request.max_rewrite_iterations,
            length_policy=length_policy,
        )
    except PlanningError as e:
        logger.exception("Full pipeline planning failed")
        raise HTTPException(
            status_code=422,
            detail={
                "error": "planning_failed",
                "message": str(e),
                "failure_reason": getattr(e, "failure_reason", None),
            },
        ) from e
    except Exception as e:
        logger.exception("Full pipeline failed")
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}") from e

    plan = planning_phase_result.content_plan
    outline = content_plan_to_outline_markdown(plan)
    return FullPipelineResponse(
        status=status,
        work_dir=str(work_dir),
        title_choices=[
            TitleChoiceResponse(title=tc.title, probability_of_success=tc.probability_of_success)
            for tc in plan.title_candidates
        ],
        outline=outline,
        draft_preview=draft_result.draft[:2000] + ("..." if len(draft_result.draft) > 2000 else ""),
        content_plan_summary=content_plan_summary_text(plan),
    )


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {
        "status": "ok",
        "brand_spec_configured": brand_spec_prompt_configured(),
    }


# ---------------------------------------------------------------------------
# Async Pipeline with Job Tracking
# ---------------------------------------------------------------------------


class BlogJobStatusResponse(BaseModel):
    """Response for job status polling."""

    job_id: str
    status: str = Field(..., description="pending, running, completed, failed, or needs_human_review")
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
    approved_at: Optional[str] = Field(None, description="When the job was approved (ISO timestamp)")
    approved_by: Optional[str] = Field(None, description="Who approved the job (optional)")
    job_type: Optional[str] = Field(None, description="Job category, e.g. medium_stats")
    content_plan_summary: Optional[str] = Field(
        None,
        description="Short summary from ContentPlan when pipeline completed planning",
    )
    planning_iterations_used: Optional[int] = Field(None, description="Planning refine iterations completed")
    parse_retry_count: Optional[int] = Field(None, description="JSON parse/repair attempts during planning")
    planning_wall_ms_total: Optional[float] = Field(None, description="Wall-clock ms spent in planning phase")
    planning_failure_reason: Optional[str] = Field(
        None,
        description="When status is failed and failed_phase is planning, machine-readable reason",
    )
    # Title selection collaboration fields
    waiting_for_title_selection: bool = Field(False, description="True when the pipeline is paused waiting for the author to select a title")
    selected_title: Optional[str] = Field(None, description="Title chosen by the author from the planning candidates")
    # Story elicitation collaboration fields
    waiting_for_story_input: bool = Field(False, description="True when the ghost writer agent is waiting for the author's story response")
    story_gaps: List[Dict[str, Any]] = Field(default_factory=list, description="Story gap opportunities identified by the ghost writer agent")
    current_story_gap_index: int = Field(0, description="Index of the story gap currently being elicited")
    story_chat_history: List[Dict[str, Any]] = Field(default_factory=list, description="Multi-turn conversation between ghost writer and author")
    elicited_stories: List[str] = Field(default_factory=list, description="Compiled first-person story narratives from the interview")
    # General Q&A collaboration fields
    pending_questions: List[Dict[str, Any]] = Field(default_factory=list, description="Questions from pipeline agents waiting for author answers")
    waiting_for_answers: bool = Field(False, description="True when the pipeline is paused waiting for Q&A answers")


def _blog_job_dict_to_status_response(job: Dict[str, Any], job_id_fallback: str) -> BlogJobStatusResponse:
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
        planning_iterations_used=job.get("planning_iterations_used"),
        parse_retry_count=job.get("parse_retry_count"),
        planning_wall_ms_total=job.get("planning_wall_ms_total"),
        planning_failure_reason=job.get("planning_failure_reason"),
        waiting_for_title_selection=bool(job.get("waiting_for_title_selection", False)),
        selected_title=job.get("selected_title"),
        waiting_for_story_input=bool(job.get("waiting_for_story_input", False)),
        story_gaps=job.get("story_gaps", []),
        current_story_gap_index=job.get("current_story_gap_index", 0),
        story_chat_history=job.get("story_chat_history", []),
        elicited_stories=job.get("elicited_stories", []),
        pending_questions=job.get("pending_questions", []),
        waiting_for_answers=bool(job.get("waiting_for_answers", False)),
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
    producer_phase: Optional[str] = Field(None, description="Pipeline phase that produced this artifact")
    producer_agent: Optional[str] = Field(None, description="Agent or component that produced this artifact")


class ArtifactListResponse(BaseModel):
    """Response listing artifact names that exist for a job, with optional producer metadata."""

    artifacts: List[ArtifactMeta] = Field(..., description="Existing artifacts with name and producer metadata")


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


def _require_medium_integration() -> None:
    ok, msg = medium_stats_integration_eligible()
    if not ok:
        raise HTTPException(status_code=503, detail=msg)


def _run_medium_stats_async_job(job_id: str, payload: MediumStatsRequest) -> None:
    """Background worker: scrape Medium stats and write medium_stats_report.json."""
    cfg = MediumStatsRunConfig(
        headless=payload.headless,
        timeout_ms=payload.timeout_ms,
        max_posts=payload.max_posts,
    )
    try:
        ok, msg = medium_stats_integration_eligible()
        if not ok:
            raise RuntimeError(msg)
        if start_blog_job is not None:
            start_blog_job(job_id)
        if get_blog_job is None:
            raise RuntimeError("Job store unavailable")
        job = get_blog_job(job_id)
        work_dir_str = job.get("work_dir") if job else None
        if not work_dir_str:
            raise RuntimeError("Medium stats job missing work_dir")
        if update_blog_job is not None:
            update_blog_job(
                job_id,
                status_text="Collecting Medium statistics…",
                progress=15,
                phase="medium_stats",
            )
        report = BlogMediumStatsAgent().collect(cfg)
        if write_artifact is None:
            raise RuntimeError("Artifact persistence not available")
        write_artifact(work_dir_str, "medium_stats_report.json", report.model_dump(mode="json"))
        if update_blog_job is not None:
            update_blog_job(
                job_id,
                status=JOB_STATUS_COMPLETED,
                phase="medium_stats",
                progress=100,
                status_text=f"Collected statistics for {len(report.posts)} posts",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        logger.info("Completed Medium stats job %s (%s posts)", job_id, len(report.posts))
    except Exception as e:
        logger.exception("Medium stats failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=str(e), failed_phase="medium_stats")


def _run_pipeline_with_tracking(job_id: str, request: FullPipelineRequest) -> None:
    """Run the full pipeline in a background thread with job tracking."""
    try:
        import sys
        from pathlib import Path
        _blogging_root = Path(__file__).resolve().parent.parent
        if str(_blogging_root) not in sys.path:
            sys.path.insert(0, str(_blogging_root))
        from agent_implementations.blog_writing_process_v2 import run_pipeline

        work_dir = RUN_ARTIFACTS_BASE / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        brief_text = request.brief.strip()
        if request.title_concept:
            brief_text = f"{brief_text}. Title concept: {request.title_concept.strip()}"
        audience_str = _format_audience(request.audience)

        brief_input = ResearchBriefInput(
            brief=brief_text,
            audience=audience_str or None,
            tone_or_purpose=request.tone_or_purpose,
            max_results=request.max_results,
        )

        def job_updater(**kwargs: Any) -> None:
            """Update job status in the job store."""
            if update_blog_job is not None:
                try:
                    update_blog_job(job_id, **kwargs)
                except Exception as e:
                    logger.warning("Failed to update job %s: %s", job_id, e)

        # Mark job as started
        if start_blog_job is not None:
            start_blog_job(job_id)
        job_updater(work_dir=str(work_dir))

        length_policy = resolve_length_policy(
            content_profile=request.content_profile,
            explicit_target_word_count=request.target_word_count,
            length_notes=request.length_notes,
            series_context=request.series_context,
        )
        try:
            _research_result, planning_phase_result, draft_result, status = run_pipeline(
                brief_input,
                work_dir=work_dir,
                run_gates=request.run_gates,
                max_rewrite_iterations=request.max_rewrite_iterations,
                job_updater=job_updater,
                job_id=job_id,
                length_policy=length_policy,
            )

            plan = planning_phase_result.content_plan
            outline = content_plan_to_outline_markdown(plan)
            title_choices = [
                {"title": tc.title, "probability_of_success": tc.probability_of_success}
                for tc in plan.title_candidates
            ]
            draft_preview = draft_result.draft[:2000] + ("..." if len(draft_result.draft) > 2000 else "")

            final_status = JOB_STATUS_COMPLETED if status == "PASS" else JOB_STATUS_NEEDS_REVIEW
            if complete_blog_job is not None:
                complete_blog_job(
                    job_id,
                    status=final_status,
                    title_choices=title_choices,
                    outline=outline,
                    draft_preview=draft_preview,
                    content_plan_summary=content_plan_summary_text(plan),
                    planning_iterations_used=planning_phase_result.planning_iterations_used,
                    parse_retry_count=planning_phase_result.parse_retry_count,
                    planning_wall_ms_total=planning_phase_result.planning_wall_ms_total,
                )

        except PlanningError as e:
            logger.exception("Pipeline planning failed for job %s", job_id)
            if fail_blog_job is not None:
                fail_blog_job(
                    job_id,
                    error=str(e),
                    failed_phase="planning",
                    planning_failure_reason=getattr(e, "failure_reason", None),
                )
        except BloggingError as e:
            logger.exception("Pipeline failed for job %s", job_id)
            if fail_blog_job is not None:
                fail_blog_job(job_id, error=str(e), failed_phase=getattr(e, "phase", None))
        except Exception as e:
            logger.exception("Unexpected error in pipeline for job %s", job_id)
            if fail_blog_job is not None:
                fail_blog_job(job_id, error=str(e))
    except Exception as e:
        logger.exception("Pipeline failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=str(e))


@app.post(
    "/full-pipeline-async",
    response_model=StartPipelineResponse,
    summary="Start full pipeline asynchronously",
    description="Starts the full blog pipeline in the background. Returns a job_id for polling status.",
)
def start_full_pipeline_async(request: FullPipelineRequest) -> StartPipelineResponse:
    """Start the full pipeline asynchronously and return job_id for polling."""
    if create_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Async pipeline not available - job store module not found",
        )

    job_id = str(uuid.uuid4())[:8]
    audience_str = _format_audience(request.audience)

    # Create job record
    create_blog_job(
        job_id,
        brief=request.brief[:500],
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
    )

    # When Temporal is enabled, start workflow for resumable state; otherwise run in thread
    try:
        from blogging.temporal.client import is_temporal_enabled
        from blogging.temporal.start_workflow import start_full_pipeline_workflow
        if is_temporal_enabled():
            request_dict = request.model_dump(mode="json")
            request_dict["audience"] = audience_str or request_dict.get("audience")
            start_full_pipeline_workflow(job_id, request_dict)
            logger.info("Started async pipeline job %s via Temporal", job_id)
            return StartPipelineResponse(job_id=job_id, message="Pipeline started (Temporal)")
    except ImportError:
        pass

    # Start pipeline in background thread
    thread = threading.Thread(
        target=_run_pipeline_with_tracking,
        args=(job_id, request),
        daemon=True,
    )
    thread.start()

    logger.info("Started async pipeline job %s", job_id)
    return StartPipelineResponse(job_id=job_id, message="Pipeline started")


@app.post(
    "/research-and-review-async",
    response_model=StartPipelineResponse,
    summary="Start research and review asynchronously",
    description="Starts the research and review pipeline in the background. Returns a job_id for polling status.",
)
def start_research_review_async(request: ResearchAndReviewRequest) -> StartPipelineResponse:
    """Start research-and-review asynchronously and return job_id for polling."""
    if create_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Async research-and-review not available - job store module not found",
        )

    job_id = str(uuid.uuid4())[:8]
    audience_str = _format_audience(request.audience)

    create_blog_job(
        job_id,
        brief=request.brief[:500],
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
    )

    thread = threading.Thread(
        target=_run_research_review_with_tracking,
        args=(job_id, request),
        daemon=True,
    )
    thread.start()

    logger.info("Started async research-and-review job %s", job_id)
    return StartPipelineResponse(job_id=job_id, message="Research and review started")


@app.post(
    "/medium-stats",
    response_model=MediumStatsReport,
    summary="Collect Medium post statistics (sync)",
    description=(
        "Runs Playwright against medium.com/me/stats. "
        "Requires the Medium.com integration: enabled and configured under /api/integrations/medium "
        "(Google OAuth identity when using Google, plus an imported Playwright browser session)."
    ),
)
def medium_stats_sync(payload: MediumStatsRequest) -> MediumStatsReport:
    """Synchronous Medium statistics scrape."""
    _require_medium_integration()
    cfg = MediumStatsRunConfig(
        headless=payload.headless,
        timeout_ms=payload.timeout_ms,
        max_posts=payload.max_posts,
    )
    try:
        return BlogMediumStatsAgent().collect(cfg)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception("Medium stats sync failed")
        raise HTTPException(status_code=500, detail=f"Medium stats failed: {e}") from e


@app.post(
    "/medium-stats-async",
    response_model=StartPipelineResponse,
    summary="Start Medium statistics collection asynchronously",
    description=(
        "Creates a job with job_type medium_stats and work_dir under medium_stats_runs. "
        "Poll GET /job/{job_id}; artifact medium_stats_report.json when status is completed."
    ),
)
def medium_stats_async(payload: MediumStatsRequest) -> StartPipelineResponse:
    """Start Medium stats job in a background thread."""
    if create_blog_job is None or medium_stats_run_dir is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available for async Medium stats",
        )
    _require_medium_integration()
    job_id = str(uuid.uuid4())[:8]
    work_dir = str(medium_stats_run_dir(job_id))
    create_blog_job(
        job_id,
        brief="Medium post statistics",
        work_dir=work_dir,
        job_type="medium_stats",
    )
    thread = threading.Thread(
        target=_run_medium_stats_async_job,
        args=(job_id, payload),
        daemon=True,
    )
    thread.start()
    logger.info("Started async Medium stats job %s", job_id)
    return StartPipelineResponse(job_id=job_id, message="Medium statistics job started")


@app.get(
    "/job/{job_id}",
    response_model=BlogJobStatusResponse,
    summary="Get job status",
    description="Poll the status of a running or completed pipeline job.",
)
def get_job_status(job_id: str) -> BlogJobStatusResponse:
    """Get the current status of a pipeline job."""
    if get_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job status not available - job store module not found",
        )

    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return _blog_job_dict_to_status_response(job, job_id)


class CancelJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


@app.post(
    "/job/{job_id}/cancel",
    response_model=CancelJobResponse,
    summary="Cancel a running or pending job",
    description="Sets job status to cancelled. Only allowed for pending or running jobs. Returns 400 for terminal states, 404 if job not found.",
)
def cancel_job(job_id: str) -> CancelJobResponse:
    """Request cancellation for a pending or running pipeline job."""
    if get_blog_job is None or update_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current = job.get("status", "pending")
    if current not in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {current}. Cannot cancel.",
        )
    update_blog_job(job_id, status="cancelled")
    return CancelJobResponse(job_id=job_id, message="Job cancellation requested.")


@app.delete(
    "/job/{job_id}",
    response_model=DeleteJobResponse,
    summary="Delete a job",
    description="Remove the job from the store. Returns 404 if job not found.",
)
def delete_job(job_id: str) -> DeleteJobResponse:
    """Delete a pipeline job by id."""
    if get_blog_job is None or delete_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not delete_blog_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteJobResponse(job_id=job_id, message="Job deleted.")


@app.post(
    "/job/{job_id}/approve",
    response_model=BlogJobStatusResponse,
    summary="Approve a completed job",
    description="Mark the job as approved. Only allowed when status is completed or needs_human_review. Returns 400 for other statuses.",
)
def approve_job(job_id: str) -> BlogJobStatusResponse:
    """Approve a pipeline job (only for completed or needs_human_review)."""
    if get_blog_job is None or approve_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    status = job.get("status", "")
    if status not in (JOB_STATUS_COMPLETED, JOB_STATUS_NEEDS_REVIEW):
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be approved: status is {status!r}. Only completed or needs_human_review jobs can be approved.",
        )
    approve_blog_job(job_id)
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after approve")
    return _blog_job_dict_to_status_response(updated, job_id)


@app.post(
    "/job/{job_id}/unapprove",
    response_model=BlogJobStatusResponse,
    summary="Unapprove a job",
    description="Clear the approval for a job. Returns updated job status.",
)
def unapprove_job(job_id: str) -> BlogJobStatusResponse:
    """Clear approval for a pipeline job."""
    if get_blog_job is None or unapprove_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    unapprove_blog_job(job_id)
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after unapprove")
    return _blog_job_dict_to_status_response(updated, job_id)


class SelectTitleRequest(BaseModel):
    """Request body for title selection."""

    title: str = Field(..., description="The author-chosen title from the planning candidates.")


@app.post(
    "/job/{job_id}/select-title",
    response_model=BlogJobStatusResponse,
    summary="Submit title selection",
    description=(
        "Resume the pipeline after title selection. "
        "Sets waiting_for_title_selection=False and records the chosen title."
    ),
)
def select_title(job_id: str, request: SelectTitleRequest) -> BlogJobStatusResponse:
    """Author submits their chosen title, resuming the pipeline."""
    if get_blog_job is None or submit_title_selection is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.get("waiting_for_title_selection"):
        raise HTTPException(status_code=400, detail="Job is not currently waiting for title selection")
    if not request.title.strip():
        raise HTTPException(status_code=422, detail="title must not be empty")
    submit_title_selection(job_id, request.title.strip())
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after title selection")
    return _blog_job_dict_to_status_response(updated, job_id)


class StoryResponseRequest(BaseModel):
    """Request body for a story elicitation response."""

    message: str = Field(..., description="The author's response to the ghost writer's question.")


@app.post(
    "/job/{job_id}/story-response",
    response_model=BlogJobStatusResponse,
    summary="Submit story elicitation response",
    description=(
        "Send a message in the ghost writer story elicitation conversation. "
        "Clears waiting_for_story_input and appends the message to story_chat_history."
    ),
)
def story_response(job_id: str, request: StoryResponseRequest) -> BlogJobStatusResponse:
    """Author submits a message in the story elicitation chat."""
    if get_blog_job is None or submit_story_user_message is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.get("waiting_for_story_input"):
        raise HTTPException(status_code=400, detail="Job is not currently waiting for a story response")
    if not request.message.strip():
        raise HTTPException(status_code=422, detail="message must not be empty")
    submit_story_user_message(job_id, request.message.strip())
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after story response")
    return _blog_job_dict_to_status_response(updated, job_id)


@app.post(
    "/job/{job_id}/skip-story-gap",
    response_model=BlogJobStatusResponse,
    summary="Skip the current story gap",
    description=(
        "Skip the current story elicitation gap and advance to the next one. "
        "Increments current_story_gap_index and clears waiting_for_story_input."
    ),
)
def skip_story_gap(job_id: str) -> BlogJobStatusResponse:
    """Author skips the current story gap."""
    if get_blog_job is None or skip_current_story_gap is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    skip_current_story_gap(job_id)
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after skip")
    return _blog_job_dict_to_status_response(updated, job_id)


class BlogAnswersRequest(BaseModel):
    """Request body for submitting Q&A answers."""

    answers: List[Dict[str, Any]] = Field(
        ...,
        description="List of answer objects (question_id, selected_option_id, selected_answer, etc.).",
    )


@app.post(
    "/job/{job_id}/answers",
    response_model=BlogJobStatusResponse,
    summary="Submit answers to pending questions",
    description=(
        "Resume the pipeline after Q&A. Stores answers, clears pending_questions, "
        "and sets waiting_for_answers=False."
    ),
)
def submit_answers(job_id: str, request: BlogAnswersRequest) -> BlogJobStatusResponse:
    """Author submits answers to pipeline Q&A questions."""
    if get_blog_job is None or submit_blog_answers is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.get("waiting_for_answers"):
        raise HTTPException(status_code=400, detail="Job is not currently waiting for answers")
    submit_blog_answers(job_id, request.answers)
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after answer submission")
    return _blog_job_dict_to_status_response(updated, job_id)


@app.get(
    "/job/{job_id}/artifacts",
    response_model=ArtifactListResponse,
    summary="List job artifacts",
    description="List artifact filenames that exist for a pipeline job. Returns 404 if the job is missing or has no work_dir.",
)
def list_job_artifacts(job_id: str) -> ArtifactListResponse:
    """List existing artifact names for a job."""
    if get_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    work_dir = job.get("work_dir")
    if not work_dir:
        raise HTTPException(status_code=404, detail="Job has no artifact directory")
    work_path = Path(work_dir)
    existing_names = [name for name in ARTIFACT_NAMES if (work_path / name).exists()]
    meta_list = []
    for name in existing_names:
        producer = ARTIFACT_PRODUCER.get(name, {}) if ARTIFACT_PRODUCER else {}
        meta_list.append(
            ArtifactMeta(
                name=name,
                producer_phase=producer.get("producer_phase"),
                producer_agent=producer.get("producer_agent"),
            )
        )
    return ArtifactListResponse(artifacts=meta_list)


@app.get(
    "/job/{job_id}/artifacts/{artifact_name}",
    summary="Get job artifact content or download",
    description="Return the content of a single artifact (JSON body), or with ?download=true return as attachment. Path traversal is blocked; artifact_name must be in the allowed list.",
    response_model=None,
)
def get_job_artifact_content(
    job_id: str,
    artifact_name: str,
    download: bool = Query(False, description="If true, return content as attachment with Content-Disposition"),
) -> ArtifactContentResponse | Response:
    """Return content of one artifact for a job, or as download attachment."""
    if get_blog_job is None or read_artifact is None:
        raise HTTPException(
            status_code=501,
            detail="Job store or artifact reader not available",
        )
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    work_dir = job.get("work_dir")
    if not work_dir:
        raise HTTPException(status_code=404, detail="Job has no artifact directory")
    if artifact_name not in ARTIFACT_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown artifact: {artifact_name!r}")
    parse_json = artifact_name.endswith(".json")
    content = read_artifact(work_dir, artifact_name, default=None, parse_json=parse_json)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_name!r} not found")

    if download:
        if isinstance(content, (dict, list)):
            raw = json_module.dumps(content, indent=2)
            media_type = "application/json"
        else:
            raw = content if isinstance(content, str) else str(content)
            if artifact_name.endswith(".json"):
                media_type = "application/json"
            elif artifact_name.endswith(".yaml") or artifact_name.endswith(".yml"):
                media_type = "text/yaml"
            else:
                media_type = "text/plain; charset=utf-8"
        return Response(
            content=raw.encode("utf-8"),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact_name}"'},
        )
    return ArtifactContentResponse(name=artifact_name, content=content)


@app.get(
    "/jobs",
    response_model=List[BlogJobListItem],
    summary="List jobs",
    description="List all pipeline jobs, optionally filtering to running jobs only.",
)
def list_jobs(running_only: bool = False) -> List[BlogJobListItem]:
    """List pipeline jobs."""
    if list_blog_jobs is None:
        raise HTTPException(
            status_code=501,
            detail="Job listing not available - job store module not found",
        )

    jobs = list_blog_jobs(running_only=running_only)
    return [
        BlogJobListItem(
            job_id=job.get("job_id", ""),
            status=job.get("status", "pending"),
            brief=job.get("brief", "")[:100],
            phase=job.get("phase"),
            progress=job.get("progress", 0),
            created_at=job.get("created_at"),
            job_type=job.get("job_type"),
        )
        for job in jobs
    ]


def _rebuild_api_models() -> None:
    """Resolve PEP 563 annotations for Pydantic (e.g. dynamic import in tests)."""
    _ns: Dict[str, Any] = {**globals()}
    for _cls in (
        AudienceDetails,
        ResearchAndReviewRequest,
        TitleChoiceResponse,
        ResearchAndReviewResponse,
        FullPipelineRequest,
        FullPipelineResponse,
        BlogJobStatusResponse,
        BlogJobListItem,
        ArtifactMeta,
        ArtifactListResponse,
        ArtifactContentResponse,
        StartPipelineResponse,
        CancelJobResponse,
        DeleteJobResponse,
    ):
        _cls.model_rebuild(_types_namespace=_ns)


_rebuild_api_models()
