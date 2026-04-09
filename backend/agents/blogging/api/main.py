"""
FastAPI application exposing the full blogging pipeline (planning -> draft -> gates).

Supports synchronous and asynchronous execution with job polling and SSE streaming for UI integration.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
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
from fastapi.responses import Response, StreamingResponse  # noqa: E402
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

from job_service_client import (  # noqa: E402
    RESTARTABLE_STATUSES,
    RESUMABLE_STATUSES,
    validate_job_for_action,
)
from llm_service import OllamaLLMClient  # noqa: E402
from shared_observability import init_otel, instrument_fastapi_app  # noqa: E402

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
        is_waiting_for_draft_feedback,
        list_blog_jobs,
        medium_stats_run_dir,
        skip_current_story_gap,
        start_blog_job,
        submit_blog_answers,
        submit_draft_feedback,
        submit_story_user_message,
        submit_title_ratings,
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
    submit_title_ratings = None
    submit_story_user_message = None
    skip_current_story_gap = None
    submit_blog_answers = None
    submit_draft_feedback = None
    is_waiting_for_draft_feedback = None
    JOB_STATUS_COMPLETED = "completed"
    JOB_STATUS_NEEDS_REVIEW = "needs_human_review"
    BloggingError = Exception

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class _QuietAccessFilter(logging.Filter):
    """Suppress noisy 200 OK access logs for health checks and polling endpoints.

    Only successful (200) requests to /health, /jobs, and /job/{id} are suppressed.
    Warnings, errors, and non-200 responses are always logged.
    """

    _QUIET_PATTERNS = ("/health", "/jobs", "/job/")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        if "200" in msg and any(p in msg for p in self._QUIET_PATTERNS):
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())

# Base directory for run artifacts (when work_dir is requested).
# Honour BLOGGING_RUN_ARTIFACTS_ROOT so Docker can mount a persistent volume.
_custom_artifacts_root = os.environ.get("BLOGGING_RUN_ARTIFACTS_ROOT")
RUN_ARTIFACTS_BASE = (
    Path(_custom_artifacts_root).expanduser().resolve()
    if _custom_artifacts_root
    else Path(tempfile.gettempdir()) / "blogging_runs"
)


def _run_blogging_service_shutdown() -> None:
    """Runs while Uvicorn still has the event loop; before process exit (replaces atexit hook)."""
    try:
        from shared.blog_job_store import stop_blog_stale_monitor

        stop_blog_stale_monitor()
    except Exception:
        logger.debug("Stale job monitor stop skipped", exc_info=True)

    logger.info("Blogging service shutdown: notifying job-service…")
    try:
        from job_service_client import JobServiceClient

        client = JobServiceClient(team="blogging_team")
        client.mark_all_active_jobs_interrupted(
            "Blogging service shutting down",
            http_timeout=5.0,
            http_max_retries=0,
        )
    except Exception as exc:
        logger.info("Job-service shutdown notification skipped: %s", exc)

    logger.info("Blogging service shutdown: stopping Temporal worker…")
    try:
        from blogging.temporal.worker import shutdown_blogging_temporal_components

        shutdown_blogging_temporal_components(worker_shutdown_timeout=8.0)
    except Exception:
        logger.warning("Temporal worker shutdown failed", exc_info=True)


@asynccontextmanager
async def _blogging_lifespan(app: FastAPI):
    # Register Postgres schema (no-op when POSTGRES_HOST is unset).
    try:
        from blogging.postgres import SCHEMA as BLOGGING_POSTGRES_SCHEMA
        from shared_postgres import register_team_schemas

        register_team_schemas(BLOGGING_POSTGRES_SCHEMA)
    except Exception:
        logger.exception("blogging postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("blogging shared_postgres close_pool failed", exc_info=True)
    _run_blogging_service_shutdown()


init_otel(service_name="blogging-team", team_key="blogging")

app = FastAPI(
    title="Blog Research & Review API",
    description="Blog pipeline: planning, drafting, and quality gates. Supports sync and async execution with job polling and SSE.",
    version="0.3.0",
    lifespan=_blogging_lifespan,
)
instrument_fastapi_app(app, team_key="blogging")


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


@app.post(
    "/full-pipeline",
    response_model=FullPipelineResponse,
    summary="Run full blog pipeline with gates",
    description="Runs planning -> draft -> validators -> compliance -> rewrite loop. Persists all artifacts.",
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
        planning_phase_result, draft_result, status = run_pipeline(
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


def _publish_terminal_event(job_id: str, event_type: str, **kwargs: Any) -> None:
    """Publish a terminal SSE event and clean up subscribers."""
    try:
        from shared.job_event_bus import cleanup_job, publish

        publish(job_id, kwargs, event_type=event_type)
        cleanup_job(job_id)
    except Exception:
        pass


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
            """Update job status in the job store and broadcast to SSE subscribers."""
            if update_blog_job is not None:
                try:
                    update_blog_job(job_id, **kwargs)
                except Exception as e:
                    logger.warning("Failed to update job %s: %s", job_id, e)
            try:
                from shared.job_event_bus import publish

                publish(job_id, kwargs, event_type="update")
            except Exception:
                pass

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
            planning_phase_result, draft_result, status = run_pipeline(
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
            draft_preview = draft_result.draft[:2000] + (
                "..." if len(draft_result.draft) > 2000 else ""
            )

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
            _publish_terminal_event(job_id, "complete", status=final_status)

        except PlanningError as e:
            logger.exception("Pipeline planning failed for job %s", job_id)
            if fail_blog_job is not None:
                fail_blog_job(
                    job_id,
                    error=str(e),
                    failed_phase="planning",
                    planning_failure_reason=getattr(e, "failure_reason", None),
                )
            _publish_terminal_event(job_id, "error", error=str(e), failed_phase="planning")
        except BloggingError as e:
            logger.exception("Pipeline failed for job %s", job_id)
            if fail_blog_job is not None:
                fail_blog_job(job_id, error=str(e), failed_phase=getattr(e, "phase", None))
            _publish_terminal_event(job_id, "error", error=str(e))
        except Exception as e:
            logger.exception("Unexpected error in pipeline for job %s", job_id)
            if fail_blog_job is not None:
                fail_blog_job(job_id, error=str(e))
            _publish_terminal_event(job_id, "error", error=str(e))
    except Exception as e:
        logger.exception("Pipeline failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=str(e))
        _publish_terminal_event(job_id, "error", error=str(e))


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

    # Create job record (store full request payload for resume/restart)
    create_blog_job(
        job_id,
        brief=request.brief[:500],
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
    )
    if update_blog_job is not None:
        update_blog_job(job_id, request_payload=request.model_dump(mode="json"))

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


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "needs_human_review"})


@app.get(
    "/job/{job_id}/stream",
    summary="Stream job status via SSE",
    description=(
        "Server-Sent Events stream for real-time job updates. "
        "Emits an initial 'snapshot' event with full status, then incremental 'update' events, "
        "and a terminal event ('complete', 'error', or 'cancelled') before closing."
    ),
)
def stream_job_status(job_id: str) -> StreamingResponse:
    """SSE stream for a pipeline job. Falls back gracefully if job is already terminal."""
    import time

    from shared.job_event_bus import subscribe, unsubscribe

    if get_blog_job is None:
        raise HTTPException(status_code=501, detail="Job store not available")

    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    def _sse_line(data: dict) -> str:
        return f"data: {json_module.dumps(data, default=str)}\n\n"

    def _snapshot_event() -> dict:
        current = get_blog_job(job_id) or {}
        resp = _blog_job_dict_to_status_response(current, job_id)
        return {"type": "snapshot", **resp.model_dump(mode="json")}

    # If the job is already terminal, send a snapshot + done and close immediately.
    if job.get("status") in _TERMINAL_STATUSES:

        def _terminal_gen():
            yield _sse_line(_snapshot_event())
            yield _sse_line({"type": "done"})

        return StreamingResponse(_terminal_gen(), media_type="text/event-stream")

    def event_generator():
        sub = subscribe(job_id)
        try:
            # Initial snapshot so the client has the full current state
            yield _sse_line(_snapshot_event())

            deadline = time.monotonic() + 4 * 3600  # 4-hour max connection
            while time.monotonic() < deadline:
                # Drain all queued events
                sent_terminal = False
                while sub.events:
                    event = sub.events.popleft()
                    yield _sse_line(event)
                    if event.get("type") in ("complete", "error", "cancelled"):
                        sent_terminal = True
                if sent_terminal:
                    yield _sse_line({"type": "done"})
                    return

                # Keepalive (SSE comment — keeps proxies from closing idle connections)
                yield ": keepalive\n\n"

                # Wait for notification or timeout after 1s
                sub.notify.wait(timeout=1.0)
                sub.notify.clear()
        finally:
            unsubscribe(job_id, sub)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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


_BLOG_RESTARTABLE = RESTARTABLE_STATUSES | {"needs_human_review"}


@app.post(
    "/job/{job_id}/resume",
    response_model=StartPipelineResponse,
    summary="Resume an interrupted blog pipeline job",
    description=(
        "Re-dispatch the pipeline for an interrupted job. The pipeline re-runs with "
        "the same inputs and work_dir, leveraging existing artifacts (planning cache, "
        "draft files) to skip completed work where possible."
    ),
)
def resume_blog_job(job_id: str) -> StartPipelineResponse:
    """Resume a blog job from its last checkpoint."""
    if get_blog_job is None or update_blog_job is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    try:
        job = validate_job_for_action(get_blog_job(job_id), job_id, RESUMABLE_STATUSES, "resumed")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    payload = job.get("request_payload")
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="Original request payload not available for resume."
        )

    update_blog_job(
        job_id, status="running", error=None, failed_phase=None, status_text="Resuming..."
    )

    request = FullPipelineRequest(**payload)

    try:
        from blogging.temporal.client import is_temporal_enabled
        from blogging.temporal.start_workflow import start_full_pipeline_workflow

        if is_temporal_enabled():
            request_dict = request.model_dump(mode="json")
            audience_str = _format_audience(request.audience)
            request_dict["audience"] = audience_str or request_dict.get("audience")
            start_full_pipeline_workflow(job_id, request_dict)
            return StartPipelineResponse(job_id=job_id, message="Job resumed (Temporal)")
    except ImportError:
        pass

    thread = threading.Thread(
        target=_run_pipeline_with_tracking,
        args=(job_id, request),
        daemon=True,
    )
    thread.start()
    return StartPipelineResponse(job_id=job_id, message="Job resumed")


@app.post(
    "/job/{job_id}/restart",
    response_model=StartPipelineResponse,
    summary="Restart a blog pipeline job from scratch",
    description="Reset the job and re-run the full pipeline with the same inputs.",
)
def restart_blog_job(job_id: str) -> StartPipelineResponse:
    """Restart a blog job from the beginning."""
    if get_blog_job is None or update_blog_job is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    try:
        job = validate_job_for_action(get_blog_job(job_id), job_id, _BLOG_RESTARTABLE, "restarted")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    payload = job.get("request_payload")
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="Original request payload not available for restart."
        )

    from blogging.shared.blog_job_store import reset_blog_job

    reset_blog_job(job_id)

    request = FullPipelineRequest(**payload)

    try:
        from blogging.temporal.client import is_temporal_enabled
        from blogging.temporal.start_workflow import start_full_pipeline_workflow

        if is_temporal_enabled():
            request_dict = request.model_dump(mode="json")
            audience_str = _format_audience(request.audience)
            request_dict["audience"] = audience_str or request_dict.get("audience")
            start_full_pipeline_workflow(job_id, request_dict)
            return StartPipelineResponse(job_id=job_id, message="Job restarted (Temporal)")
    except ImportError:
        pass

    thread = threading.Thread(
        target=_run_pipeline_with_tracking,
        args=(job_id, request),
        daemon=True,
    )
    thread.start()
    return StartPipelineResponse(job_id=job_id, message="Job restarted from scratch")


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
        raise HTTPException(
            status_code=400, detail="Job is not currently waiting for title selection"
        )
    if not request.title.strip():
        raise HTTPException(status_code=422, detail="title must not be empty")
    submit_title_selection(job_id, request.title.strip())
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after title selection")
    return _blog_job_dict_to_status_response(updated, job_id)


class TitleRatingItem(BaseModel):
    """A single title rating."""

    title: str
    rating: str = Field(..., description="One of: dislike, like, love")


class RateTitlesRequest(BaseModel):
    """Request body for title ratings."""

    ratings: List[TitleRatingItem]


@app.post(
    "/job/{job_id}/rate-titles",
    response_model=BlogJobStatusResponse,
    summary="Rate title candidates",
    description=(
        "Rate each title as dislike, like, or love. "
        "If any title is loved, it becomes the selected title. "
        "Otherwise the pipeline generates new candidates based on the feedback."
    ),
)
def rate_titles(job_id: str, request: RateTitlesRequest) -> BlogJobStatusResponse:
    """Submit title ratings. Love = select that title. Like/Dislike = generate more."""
    if get_blog_job is None or submit_title_ratings is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.get("waiting_for_title_selection"):
        raise HTTPException(
            status_code=400, detail="Job is not currently waiting for title selection"
        )
    if not request.ratings:
        raise HTTPException(status_code=422, detail="At least one rating is required")
    for r in request.ratings:
        if r.rating not in ("dislike", "like", "love"):
            raise HTTPException(status_code=422, detail=f"Invalid rating: {r.rating}")

    ratings_dicts = [{"title": r.title, "rating": r.rating} for r in request.ratings]
    submit_title_ratings(job_id, ratings_dicts)

    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after rating submission")
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
        raise HTTPException(
            status_code=400, detail="Job is not currently waiting for a story response"
        )
    if not request.message.strip():
        raise HTTPException(status_code=422, detail="message must not be empty")
    submit_story_user_message(job_id, request.message.strip())
    # Notify the ghost writer's event subscription so it wakes immediately
    try:
        from shared.job_event_bus import publish

        publish(job_id, {"story_response_received": True}, event_type="story_update")
    except Exception:
        pass  # event bus is optional — polling fallback still works
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
    logger.info("Skipping current story gap for job %s", job_id)
    skip_current_story_gap(job_id)
    # Notify the ghost writer's event subscription so it wakes immediately
    try:
        from shared.job_event_bus import publish

        publish(job_id, {"story_gap_skipped": True}, event_type="story_update")
    except Exception:
        pass  # event bus is optional — polling fallback still works
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


@app.post(
    "/job/{job_id}/draft-feedback",
    response_model=BlogJobStatusResponse,
    summary="Submit draft feedback or approval",
    description=(
        "Resume the pipeline after the editor reviews a draft. "
        "Sets waiting_for_draft_feedback=False and stores the feedback. "
        "When approved=true, the draft proceeds without further revision."
    ),
)
def draft_feedback(job_id: str, request: DraftFeedbackRequest) -> BlogJobStatusResponse:
    """Editor submits feedback on a draft or approves it."""
    if get_blog_job is None or submit_draft_feedback is None:
        raise HTTPException(status_code=501, detail="Job store not available")
    job = get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.get("waiting_for_draft_feedback"):
        raise HTTPException(
            status_code=400, detail="Job is not currently waiting for draft feedback"
        )
    submit_draft_feedback(job_id, request.feedback, request.approved)
    updated = get_blog_job(job_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Job not found after feedback submission")
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
    download: bool = Query(
        False, description="If true, return content as attachment with Content-Disposition"
    ),
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
        TitleChoiceResponse,
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
        DraftFeedbackRequest,
    ):
        _cls.model_rebuild(_types_namespace=_ns)


# ── Story Bank endpoints ─────────────────────────────────────────────────────


@app.get("/stories", tags=["story-bank"])
def list_stories(limit: int = 50, offset: int = 0) -> list:
    """List all persisted author stories, newest first."""
    from shared.story_bank import list_stories as _list

    return _list(limit=limit, offset=offset)


@app.get("/stories/{story_id}", tags=["story-bank"])
def get_story(story_id: str) -> dict:
    """Retrieve a single story by ID."""
    from shared.story_bank import get_story as _get

    result = _get(story_id)
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Story not found")
    return result


@app.delete("/stories/{story_id}", tags=["story-bank"])
def delete_story(story_id: str) -> dict:
    """Delete a story from the bank."""
    from shared.story_bank import delete_story as _delete

    if not _delete(story_id):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Story not found")
    return {"deleted": True}


@app.get("/stories/search/{keywords}", tags=["story-bank"])
def search_stories(keywords: str, limit: int = 5) -> list:
    """Search stories by comma-separated keywords."""
    from shared.story_bank import find_relevant_stories

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    return find_relevant_stories(kw_list, limit=limit)


_rebuild_api_models()
