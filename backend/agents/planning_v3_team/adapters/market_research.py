"""
Adapter to call the Market Research team API for user/customer discovery.

Submits a job and polls until it completes; optional fallback when unavailable.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 2.0
_TOTAL_TIMEOUT_S = 600.0
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


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
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_S) as client:
            submit = client.post(f"{root}/market-research/run", json=payload)
            submit.raise_for_status()
            job_id = submit.json().get("job_id")
            if not job_id:
                logger.warning("Market research submit returned no job_id")
                return None

            deadline = time.monotonic() + _TOTAL_TIMEOUT_S
            while True:
                status = client.get(f"{root}/market-research/status/{job_id}")
                status.raise_for_status()
                data = status.json()
                if data.get("status") in _TERMINAL_STATUSES:
                    break
                if time.monotonic() >= deadline:
                    logger.warning("Market research job %s timed out", job_id)
                    return None
                time.sleep(_POLL_INTERVAL_S)
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        logger.warning("Market research request failed: %s", e)
        return None

    if data.get("status") != "completed":
        logger.warning(
            "Market research job %s ended with status %s: %s",
            job_id,
            data.get("status"),
            data.get("error"),
        )
        return None
    return data.get("result") or {}


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
