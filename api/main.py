"""
FastAPI application exposing the research-and-review pipeline as an HTTP endpoint.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from blog_research_agent.agent import ResearchAgent
from blog_research_agent.agent_cache import AgentCache
from blog_research_agent.llm import OllamaLLMClient
from blog_research_agent.models import ResearchBriefInput
from blog_review_agent import BlogReviewAgent, BlogReviewInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Blog Research & Review API",
    description="Runs research and review agents to produce title choices and a blog outline from a brief.",
    version="0.1.0",
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


def _get_agents() -> tuple[ResearchAgent, BlogReviewAgent]:
    """Lazily initialize and return research and review agents."""
    global _llm_client, _research_agent, _review_agent
    if _llm_client is None:
        _llm_client = OllamaLLMClient(model="deepseek-r1", timeout=1800.0)
    if _research_agent is None:
        cache = AgentCache(cache_dir=".agent_cache")
        _research_agent = ResearchAgent(llm_client=_llm_client, cache=cache)
    if _review_agent is None:
        _review_agent = BlogReviewAgent(llm_client=_llm_client)
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
    except Exception as e:
        logger.exception("Failed to initialize agents")
        raise HTTPException(status_code=500, detail=f"Agent initialization failed: {e}") from e

    llm_requests_before = _llm_client.request_count if _llm_client is not None else 0

    # Build brief text (include title concept if provided)
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

    llm_requests_after = _llm_client.request_count if _llm_client is not None else llm_requests_before
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


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
