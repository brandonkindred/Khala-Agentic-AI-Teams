"""Blogging API — full pipeline (sync + async) and the health check."""

from __future__ import annotations

import logging
import uuid

from agents.blogging.api.background import (
    _import_run_pipeline,
    _prepare_pipeline_input,
    _run_pipeline_with_tracking,
)
from agents.blogging.api.dependencies import require_web_search_configured
from agents.blogging.api.models import (
    FullPipelineRequest,
    FullPipelineResponse,
    StartPipelineResponse,
    TitleChoiceResponse,
    _format_audience,
)
from agents.blogging.blog_research_agent.tools.web_search import is_web_search_configured
from agents.blogging.shared.brand_spec import brand_spec_prompt_configured
from agents.blogging.shared.content_plan import (
    content_plan_summary_text,
    content_plan_to_outline_markdown,
)
from agents.blogging.shared.errors import PlanningError
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/full-pipeline",
    response_model=FullPipelineResponse,
    summary="Run full blog pipeline with gates",
    description=(
        "Runs planning -> draft -> validators -> compliance -> rewrite loop. Persists all "
        "artifacts. Rejects with 422 web_search_not_configured if OLLAMA_API_KEY is unset "
        "(see GET /health)."
    ),
)
def full_pipeline(request: FullPipelineRequest) -> FullPipelineResponse:
    """Run the full brand-aligned pipeline with artifact persistence and gates.

    Raises:
        HTTPException(422, "web_search_not_configured"): when OLLAMA_API_KEY is unset.
    """
    from agents.blogging.api import main as _main

    require_web_search_configured()

    run_pipeline = _import_run_pipeline()

    run_id = str(uuid.uuid4())[:8]
    work_dir = _main.RUN_ARTIFACTS_BASE / run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    brief_input, length_policy = _prepare_pipeline_input(request)
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
        draft_preview=draft_result.draft,
        content_plan_summary=content_plan_summary_text(plan),
    )


@router.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {
        "status": "ok",
        "brand_spec_configured": brand_spec_prompt_configured(),
        "web_search_configured": is_web_search_configured(),
    }


@router.post(
    "/full-pipeline-async",
    response_model=StartPipelineResponse,
    summary="Start full pipeline asynchronously",
    description=(
        "Starts the full blog pipeline in the background. Returns a job_id for polling "
        "status. Rejects with 422 web_search_not_configured if OLLAMA_API_KEY is unset "
        "(see GET /health)."
    ),
)
def start_full_pipeline_async(request: FullPipelineRequest) -> StartPipelineResponse:
    """Start the full pipeline asynchronously and return job_id for polling.

    Raises:
        HTTPException(501): when the job store module is unavailable -- checked
            before the web-search precondition, so a broken deployment is
            reported as unavailable rather than as a configuration error.
        HTTPException(422, "web_search_not_configured"): when OLLAMA_API_KEY is unset.
    """
    from agents.blogging.api import main as _main

    if _main.create_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Async pipeline not available - job store module not found",
        )

    require_web_search_configured()

    job_id = str(uuid.uuid4())[:8]
    audience_str = _format_audience(request.audience)

    # Create job record (store full request payload for resume/restart)
    _main.create_blog_job(
        job_id,
        brief=request.brief,
        audience=audience_str or None,
        tone_or_purpose=request.tone_or_purpose,
    )
    if _main.update_blog_job is not None:
        _main.update_blog_job(job_id, request_payload=request.model_dump(mode="json"))

    # When Temporal is enabled, start workflow for resumable state; otherwise run in thread
    try:
        from agents.blogging.temporal.start_workflow import start_full_pipeline_workflow

        from shared.temporal.client import is_temporal_enabled

        if is_temporal_enabled():
            request_dict = request.model_dump(mode="json")
            request_dict["audience"] = audience_str or request_dict.get("audience")
            start_full_pipeline_workflow(job_id, request_dict)
            logger.info("Started async pipeline job %s via Temporal", job_id)
            return StartPipelineResponse(job_id=job_id, message="Pipeline started (Temporal)")
    except ImportError:
        pass

    # Submit to the bounded async-job pool (Temporal is preferred when enabled above).
    _main._submit_async_job(_run_pipeline_with_tracking, job_id, request)

    logger.info("Started async pipeline job %s", job_id)
    return StartPipelineResponse(job_id=job_id, message="Pipeline started")
