"""
Adapter to call the Market Research team API for user/customer discovery.

Submits a job and polls until it completes; optional fallback when unavailable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from planning_team.adapters._base import BaseAdapter
from shared.http.job_polling import get_json, poll_until_terminal, post_json

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 2.0
_TOTAL_TIMEOUT_S = 600.0

_adapter = BaseAdapter(
    env_var="PLANNING_MARKET_RESEARCH_URL",
    path_prefix="/api/market-research",
    unconfigured_log="market research",
)


def request_market_research(
    product_concept: str,
    target_users: str,
    business_goal: str,
    human_approved: bool = True,
    human_feedback: str = "Planning requested user/customer discovery.",
) -> Optional[Dict[str, Any]]:
    """
    Submit a market-research job and poll until it completes. Returns the
    completed ``result`` dict (mission_summary, insights, etc.) or ``None``
    on any failure (service unavailable, timeout, non-completed terminal
    status).
    """
    run_url = _adapter.build_url("/market-research/run")
    if not run_url:
        return None
    payload = {
        "product_concept": product_concept,
        "target_users": target_users,
        "business_goal": business_goal,
        "human_approved": human_approved,
        "human_feedback": human_feedback,
    }

    submitted = post_json(
        run_url,
        payload,
        timeout=_REQUEST_TIMEOUT_S,
        log_context="Market research submit",
    )
    if not submitted:
        return None
    job_id = submitted.get("job_id")
    if not job_id:
        logger.warning("Market research submit returned no job_id")
        return None

    result = poll_until_terminal(
        lambda: get_json(
            _adapter.build_url(f"/market-research/status/{job_id}"),
            timeout=_REQUEST_TIMEOUT_S,
            log_context=f"Market research status for {job_id}",
        ),
        poll_interval=_POLL_INTERVAL_S,
        total_timeout=_TOTAL_TIMEOUT_S,
        log_context=f"market research job {job_id}",
    )
    if result.get("status") != "completed":
        logger.warning(
            "Market research job %s ended with status %s: %s",
            job_id,
            result.get("status"),
            result.get("error"),
        )
        return None
    return result.get("result") or {}


class _RecommendationSchema(BaseModel):
    """Subset of Market Research TeamOutput.recommendation consumed by this adapter."""

    model_config = ConfigDict(extra="ignore")

    rationale: List[str] = Field(default_factory=list)


class _InsightSchema(BaseModel):
    """Subset of Market Research TeamOutput.insights[] consumed by this adapter."""

    model_config = ConfigDict(extra="ignore")

    pain_points: List[str] = Field(default_factory=list)


class _MarketSignalSchema(BaseModel):
    """Subset of Market Research TeamOutput.market_signals[] consumed by this adapter."""

    model_config = ConfigDict(extra="ignore")

    signal: Optional[str] = None


class _MarketResearchResultSchema(BaseModel):
    """
    Contract for the Market Research job result (Market Research TeamOutput.model_dump()),
    scoped to the fields this adapter maps into evidence.
    """

    model_config = ConfigDict(extra="ignore")

    mission_summary: str = ""
    recommendation: Optional[_RecommendationSchema] = None
    insights: List[_InsightSchema] = Field(default_factory=list)
    market_signals: List[_MarketSignalSchema] = Field(default_factory=list)


def market_research_to_evidence(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map Market Research TeamOutput to a compact evidence dict for context/synthesis.
    """
    try:
        parsed = _MarketResearchResultSchema.model_validate(data)
    except ValidationError as exc:
        logger.warning("Market research result failed validation: %s", exc)
        parsed = _MarketResearchResultSchema()

    insights: List[str] = []
    if parsed.recommendation:
        insights.extend(parsed.recommendation.rationale)
    for item in parsed.insights:
        insights.extend(item.pain_points)
    signals = [s.signal for s in parsed.market_signals if s.signal]

    return {
        "summary": parsed.mission_summary,
        "insights": insights,
        "market_signals": signals,
        "source": "market_research_team",
    }
