"""
Adapter to call the Market Research team API for user/customer discovery.

Submits a job and polls until it completes; optional fallback when unavailable.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from shared.http.job_polling import get_json, poll_until_terminal, post_json

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 2.0
_TOTAL_TIMEOUT_S = 600.0


def _base_url() -> Optional[str]:
    return os.environ.get("PLANNING_MARKET_RESEARCH_URL") or os.environ.get("UNIFIED_API_BASE_URL")


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
    base = _base_url()
    if not base:
        logger.debug("No base URL for market research; skipping.")
        return None
    root = f"{base.rstrip('/')}/api/market-research"
    payload = {
        "product_concept": product_concept,
        "target_users": target_users,
        "business_goal": business_goal,
        "human_approved": human_approved,
        "human_feedback": human_feedback,
    }

    submitted = post_json(
        f"{root}/market-research/run",
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
            f"{root}/market-research/status/{job_id}",
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


def market_research_to_evidence(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map Market Research TeamOutput to a compact evidence dict for context/synthesis.
    """
    summary = data.get("mission_summary", "")
    insights = []
    rec = data.get("recommendation") or {}
    if isinstance(rec, dict) and rec.get("rationale"):
        insights.extend(
            rec["rationale"] if isinstance(rec["rationale"], list) else [rec["rationale"]]
        )
    for item in data.get("insights", []):
        if isinstance(item, dict) and item.get("pain_points"):
            insights.extend(item["pain_points"])
    signals = [
        s.get("signal")
        for s in data.get("market_signals", [])
        if isinstance(s, dict) and s.get("signal")
    ]
    return {
        "summary": summary if summary else "",
        "insights": insights,
        "market_signals": signals,
        "source": "market_research_team",
    }
