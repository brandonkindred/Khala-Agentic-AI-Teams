"""
FastAPI application exposing the research-and-planning pipeline as an HTTP endpoint.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List, Optional, Union

from blog_research_agent.models import ResearchBriefInput
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from shared.content_plan import content_plan_to_outline_markdown
from shared.content_profile import resolve_length_policy
from shared.errors import PlanningError

from llm_service import get_client

_blogging_root = Path(__file__).resolve().parent.parent / "blogging"
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Blog Research & Planning API",
    description="Runs research and structured content planning to produce title choices and an outline.",
    version="0.2.0",
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
    """Request body for the research-and-planning endpoint."""

    brief: str = Field(..., description="Short description of the content topic.")
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


class TitleChoiceResponse(BaseModel):
    """A title choice with probability of success."""

    title: str
    probability_of_success: float


class ResearchAndReviewResponse(BaseModel):
    """Response from the research-and-planning endpoint."""

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


@app.post(
    "/research-and-review",
    response_model=ResearchAndReviewResponse,
    summary="Run research and planning pipeline",
    description="Executes research then structured planning (same as blogging service).",
)
def research_and_review(request: ResearchAndReviewRequest) -> ResearchAndReviewResponse:
    """
    Run research then content planning.

    Accepts a brief, optional title concept, and audience details.
    """
    try:
        llm_client = get_client("blog")
    except Exception as e:
        logger.exception("Failed to initialize LLM client")
        raise HTTPException(status_code=500, detail=f"Agent initialization failed: {e}") from e

    llm_requests_before = getattr(llm_client, "request_count", 0)

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

    length_policy = resolve_length_policy()

    from agent_implementations.blog_writing_process_v2 import run_planning

    try:
        planning_phase_result = run_planning(
            brief_input,
            work_dir=None,
            llm_client=llm_client,
            length_policy=length_policy,
            series_context=None,
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

    llm_requests_after = getattr(llm_client, "request_count", llm_requests_before)
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
        compiled_document=None,
        notes=None,
    )


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
