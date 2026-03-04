"""
FastAPI application exposing the research-and-review and full pipeline as HTTP endpoints.

Supports both synchronous and asynchronous execution with job polling for UI integration.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from blog_research_agent.agent import ResearchAgent
from blog_research_agent.agent_cache import AgentCache
from blog_research_agent.llm import OllamaLLMClient
from blog_research_agent.models import ResearchBriefInput
from blog_review_agent import BlogReviewAgent, BlogReviewInput

try:
    from shared.artifacts import ARTIFACT_NAMES, read_artifact, write_artifact
except ImportError:
    ARTIFACT_NAMES = ()
    read_artifact = None
    write_artifact = None

try:
    from shared.blog_job_store import (
        create_blog_job,
        get_blog_job,
        list_blog_jobs,
        update_blog_job,
        start_blog_job,
        complete_blog_job,
        fail_blog_job,
        JOB_STATUS_COMPLETED,
        JOB_STATUS_NEEDS_REVIEW,
    )
    from shared.errors import BloggingError
except ImportError:
    create_blog_job = None
    get_blog_job = None
    list_blog_jobs = None
    update_blog_job = None
    start_blog_job = None
    complete_blog_job = None
    fail_blog_job = None
    JOB_STATUS_COMPLETED = "completed"
    JOB_STATUS_NEEDS_REVIEW = "needs_human_review"
    BloggingError = Exception

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_TAVILY_KEY_PRESENT = bool(os.environ.get("TAVILY_API_KEY"))
if not _TAVILY_KEY_PRESENT:
    logger.warning(
        "TAVILY_API_KEY is not set. The research agent requires this key for "
        "web search. Set it before calling /research-and-review or /full-pipeline."
    )

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


# Shared LLM client and agents (initialized on first request or at startup)
_llm_client: Optional[OllamaLLMClient] = None
_research_agent: Optional[ResearchAgent] = None
_review_agent: Optional[BlogReviewAgent] = None


def _get_llm_client() -> OllamaLLMClient:
    """Get or create the shared LLM client."""
    global _llm_client
    if _llm_client is None:
        _llm_client = OllamaLLMClient()
    return _llm_client


def _get_agents() -> tuple[ResearchAgent, BlogReviewAgent]:
    """Lazily initialize and return research and review agents."""
    global _research_agent, _review_agent
    llm_client = _get_llm_client()
    if _research_agent is None:
        cache = AgentCache(cache_dir=".agent_cache")
        _research_agent = ResearchAgent(llm_client=llm_client, cache=cache)
    if _review_agent is None:
        _review_agent = BlogReviewAgent(llm_client=llm_client)
    return _research_agent, _review_agent


@app.post(
    "/research-and-review",
    response_model=ResearchAndReviewResponse,
    summary="Run research and review pipeline",
    description="Executes the research agent (web + arXiv search) and review agent to produce title choices and a blog outline from the given brief and audience details.",
)
def research_and_review(request: ResearchAndReviewRequest) -> ResearchAndReviewResponse:
    """
    Run the research-and-review pipeline.

    Accepts a brief, optional title concept, and audience details. Returns
    title choices, a blog outline, and the compiled research document.
    """
    try:
        research_agent, review_agent = _get_agents()
        llm_client = _get_llm_client()
    except Exception as e:
        logger.exception("Failed to initialize agents")
        raise HTTPException(status_code=500, detail=f"Agent initialization failed: {e}") from e

    llm_requests_before = llm_client.request_count

    # Build brief text (include title concept if provided)
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

    try:
        research_result = research_agent.run(brief_input)
    except Exception as e:
        logger.exception("Research agent failed")
        raise HTTPException(status_code=500, detail=f"Research failed: {e}") from e

    try:
        review_input = BlogReviewInput(
            brief=request.brief,
            audience=audience_str or None,
            tone_or_purpose=request.tone_or_purpose,
            references=research_result.references,
        )
        review_result = review_agent.run(review_input)
    except Exception as e:
        logger.exception("Review agent failed")
        raise HTTPException(status_code=500, detail=f"Review failed: {e}") from e

    if work_dir is not None and write_artifact is not None:
        research_doc = research_result.compiled_document or ""
        if not research_doc and research_result.references:
            parts = ["## Sources\n"]
            for ref in research_result.references:
                parts.append(f"- **{ref.title}** ({ref.url}): {ref.summary}")
            research_doc = "\n".join(parts)
        write_artifact(work_dir, "research_packet.md", research_doc)
        write_artifact(work_dir, "outline.md", review_result.outline)
        logger.info("Persisted artifacts to %s", work_dir)

    llm_requests_after = llm_client.request_count
    logger.info(
        "Completed research-and-review pipeline with %s LLM requests",
        llm_requests_after - llm_requests_before,
    )

    return ResearchAndReviewResponse(
        title_choices=[
            TitleChoiceResponse(
                title=tc.title,
                probability_of_success=tc.probability_of_success,
            )
            for tc in review_result.title_choices
        ],
        outline=review_result.outline,
        compiled_document=research_result.compiled_document,
        notes=research_result.notes,
    )


def _run_research_review_with_tracking(job_id: str, request: ResearchAndReviewRequest) -> None:
    """Run the research-and-review pipeline in a background thread with job tracking."""
    try:
        research_agent, review_agent = _get_agents()
    except Exception as e:
        logger.exception("Failed to initialize agents for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=f"Agent initialization failed: {e}")
        return

    brief_text = request.brief.strip()
    if request.title_concept:
        brief_text = f"{brief_text}. Title concept: {request.title_concept.strip()}"
    audience_str = _format_audience(request.audience)

    work_dir = RUN_ARTIFACTS_BASE / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    brief_input = ResearchBriefInput(
        brief=brief_text,
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
        max_results=request.max_results,
    )

    if start_blog_job is not None:
        start_blog_job(job_id)
    if update_blog_job is not None:
        try:
            update_blog_job(job_id, phase="research", progress=10)
        except Exception as e:
            logger.warning("Failed to update job %s: %s", job_id, e)

    try:
        research_result = research_agent.run(brief_input)
    except Exception as e:
        logger.exception("Research agent failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=f"Research failed: {e}")
        return

    if update_blog_job is not None:
        try:
            update_blog_job(job_id, phase="review", progress=50)
        except Exception as e:
            logger.warning("Failed to update job %s: %s", job_id, e)

    try:
        review_input = BlogReviewInput(
            brief=request.brief,
            audience=audience_str or None,
            tone_or_purpose=request.tone_or_purpose,
            references=research_result.references,
        )
        review_result = review_agent.run(review_input)
    except Exception as e:
        logger.exception("Review agent failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=f"Review failed: {e}")
        return

    if work_dir is not None and write_artifact is not None:
        research_doc = research_result.compiled_document or ""
        if not research_doc and research_result.references:
            parts = ["## Sources\n"]
            for ref in research_result.references:
                parts.append(f"- **{ref.title}** ({ref.url}): {ref.summary}")
            research_doc = "\n".join(parts)
        write_artifact(work_dir, "research_packet.md", research_doc)
        write_artifact(work_dir, "outline.md", review_result.outline)
        logger.info("Persisted artifacts to %s", work_dir)

    title_choices = [
        {"title": tc.title, "probability_of_success": tc.probability_of_success}
        for tc in review_result.title_choices
    ]
    if complete_blog_job is not None:
        complete_blog_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            title_choices=title_choices,
            outline=review_result.outline,
        )
    logger.info("Completed research-and-review job %s", job_id)


class FullPipelineRequest(BaseModel):
    """Request body for the full pipeline endpoint."""

    brief: str = Field(..., max_length=50_000, description="Short description of the content topic.")
    title_concept: Optional[str] = Field(None, description="Optional idea or angle for the title.")
    audience: Optional[Union[AudienceDetails, str]] = Field(None, description="Audience details.")
    tone_or_purpose: Optional[str] = Field(None, description="e.g. 'educational', 'technical deep-dive'.")
    max_results: int = Field(20, ge=1, le=50, description="Maximum references.")
    run_gates: bool = Field(True, description="Run validators, fact-check, and compliance gates.")
    max_rewrite_iterations: int = Field(3, ge=1, le=10, description="Max rewrite iterations on FAIL.")


class FullPipelineResponse(BaseModel):
    """Response from the full pipeline endpoint."""

    status: str = Field(..., description="PASS, FAIL, or NEEDS_HUMAN_REVIEW.")
    work_dir: str = Field(..., description="Path to artifact directory.")
    title_choices: List[TitleChoiceResponse] = Field(default_factory=list)
    outline: str = ""
    draft_preview: Optional[str] = Field(None, description="First 2000 chars of draft.")


@app.post(
    "/full-pipeline",
    response_model=FullPipelineResponse,
    summary="Run full blog pipeline with gates",
    description="Runs research -> review -> draft -> validators -> compliance -> rewrite loop. Persists all artifacts.",
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

    try:
        research_result, review_result, draft_result, status = run_pipeline(
            brief_input,
            work_dir=work_dir,
            run_gates=request.run_gates,
            max_rewrite_iterations=request.max_rewrite_iterations,
        )
    except Exception as e:
        logger.exception("Full pipeline failed")
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}") from e

    return FullPipelineResponse(
        status=status,
        work_dir=str(work_dir),
        title_choices=[
            TitleChoiceResponse(title=tc.title, probability_of_success=tc.probability_of_success)
            for tc in review_result.title_choices
        ],
        outline=review_result.outline,
        draft_preview=draft_result.draft[:2000] + ("..." if len(draft_result.draft) > 2000 else ""),
    )


@app.get("/health")
def health() -> dict:
    """Health check endpoint. Includes a warning when TAVILY_API_KEY is missing."""
    warnings = []
    if not _TAVILY_KEY_PRESENT:
        warnings.append("TAVILY_API_KEY is not set; research agent will fail")
    result: dict = {"status": "ok"}
    if warnings:
        result["warnings"] = warnings
    return result


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


class BlogJobListItem(BaseModel):
    """Summary item for job listing."""

    job_id: str
    status: str
    brief: str = Field(..., description="First 100 chars of the brief")
    phase: Optional[str] = None
    progress: int = 0
    created_at: Optional[str] = None


class ArtifactListResponse(BaseModel):
    """Response listing artifact names that exist for a job."""

    artifacts: List[str] = Field(..., description="Names of existing artifact files")


class ArtifactContentResponse(BaseModel):
    """Response with the content of a single artifact (string for .md/.yaml, object for .json)."""

    name: str = Field(..., description="Artifact filename")
    content: Union[str, Dict[str, Any]] = Field(..., description="Artifact content as string or parsed JSON")


class StartPipelineResponse(BaseModel):
    """Response from starting an async pipeline."""

    job_id: str
    message: str = "Pipeline started"


def _run_pipeline_with_tracking(job_id: str, request: FullPipelineRequest) -> None:
    """Run the full pipeline in a background thread with job tracking."""
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

    try:
        research_result, review_result, draft_result, status = run_pipeline(
            brief_input,
            work_dir=work_dir,
            run_gates=request.run_gates,
            max_rewrite_iterations=request.max_rewrite_iterations,
            job_updater=job_updater,
        )

        # Mark job as completed
        title_choices = [
            {"title": tc.title, "probability_of_success": tc.probability_of_success}
            for tc in review_result.title_choices
        ]
        draft_preview = draft_result.draft[:2000] + ("..." if len(draft_result.draft) > 2000 else "")

        final_status = JOB_STATUS_COMPLETED if status == "PASS" else JOB_STATUS_NEEDS_REVIEW
        if complete_blog_job is not None:
            complete_blog_job(
                job_id,
                status=final_status,
                title_choices=title_choices,
                outline=review_result.outline,
                draft_preview=draft_preview,
            )

    except BloggingError as e:
        logger.exception("Pipeline failed for job %s", job_id)
        if fail_blog_job is not None:
            fail_blog_job(job_id, error=str(e), failed_phase=getattr(e, "phase", None))
    except Exception as e:
        logger.exception("Unexpected error in pipeline for job %s", job_id)
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

    # Convert title_choices from dict to response model
    title_choices = []
    for tc in job.get("title_choices", []):
        if isinstance(tc, dict):
            title_choices.append(TitleChoiceResponse(
                title=tc.get("title", ""),
                probability_of_success=tc.get("probability_of_success", 0.0),
            ))

    return BlogJobStatusResponse(
        job_id=job.get("job_id", job_id),
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
    )


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
    existing = [name for name in ARTIFACT_NAMES if (work_path / name).exists()]
    return ArtifactListResponse(artifacts=existing)


@app.get(
    "/job/{job_id}/artifacts/{artifact_name}",
    response_model=ArtifactContentResponse,
    summary="Get job artifact content",
    description="Return the content of a single artifact. Path traversal is blocked; artifact_name must be in the allowed list.",
)
def get_job_artifact_content(job_id: str, artifact_name: str) -> ArtifactContentResponse:
    """Return content of one artifact for a job."""
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
        )
        for job in jobs
    ]
