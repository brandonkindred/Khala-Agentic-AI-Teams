"""
Adapter to call the Market Research team API for user/customer discovery.

POST /api/market-research/market-research/run. Optional fallback when unavailable.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0


def _base_url() -> Optional[str]:
    return os.environ.get("PLANNING_V3_MARKET_RESEARCH_URL") or os.environ.get(
        "UNIFIED_API_BASE_URL"
    )


def request_market_research(
    product_concept: str,
    target_users: str,
    business_goal: str,
    human_approved: bool = True,
    human_feedback: str = "Planning V3 requested user/customer discovery.",
) -> Optional[Dict[str, Any]]:
    """
    Call Market Research API. Returns response data (mission_summary, insights, etc.)
    or None on failure (e.g. service unavailable). Use for evidence/synthesis.
    """
    base = _base_url()
    if not base:
        logger.debug("No base URL for market research; skipping.")
        return None
    base = base.rstrip("/")
    url = f"{base}/api/market-research/market-research/run"
    payload = {
        "product_concept": product_concept,
        "target_users": target_users,
        "business_goal": business_goal,
        "human_approved": human_approved,
        "human_feedback": human_feedback,
    }
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        logger.warning("Market research request failed: %s", e)
        return None


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
        "summary": summary[:2000] if summary else "",
        "insights": insights[:30],
        "market_signals": signals[:20],
        "source": "market_research_team",
    }
