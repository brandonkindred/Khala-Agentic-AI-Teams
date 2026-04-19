"""Adapter to call the Market Research team API for competitive/similar-brands research."""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from branding_team.models import BrandingMission, CompetitiveSnapshot

_POLL_INTERVAL_S = 2.0
_TOTAL_TIMEOUT_S = 600.0
_REQUEST_TIMEOUT_S = 30.0
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _base_url() -> Optional[str]:
    return os.environ.get("UNIFIED_API_BASE_URL") or os.environ.get("BRANDING_MARKET_RESEARCH_URL")


def request_market_research(mission: BrandingMission) -> Optional[CompetitiveSnapshot]:
    """
    Submit a market-research job and poll until it completes; return
    CompetitiveSnapshot or None when the service is unavailable. Raises
    RuntimeError on transport/parse errors or terminal job failure.
    """
    base = _base_url()
    if not base:
        return None
    root = f"{base.rstrip('/')}/api/market-research"
    product_concept = (
        f"Competitive and similar brands for {mission.company_name}: {mission.company_description}"
    )
    target_users = mission.target_audience
    differentiators = (
        ", ".join(mission.differentiators) if mission.differentiators else "differentiate"
    )
    business_goal = f"Differentiate and position brand. Key differentiators: {differentiators}"

    payload = {
        "product_concept": product_concept,
        "target_users": target_users,
        "business_goal": business_goal,
        "human_approved": True,
        "human_feedback": "Branding team requested competitive snapshot.",
    }
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_S) as client:
            submit = client.post(f"{root}/market-research/run", json=payload)
            submit.raise_for_status()
            job_id = submit.json().get("job_id")
            if not job_id:
                raise RuntimeError("Market research submit returned no job_id")

            deadline = time.monotonic() + _TOTAL_TIMEOUT_S
            while True:
                status = client.get(f"{root}/market-research/status/{job_id}")
                status.raise_for_status()
                data = status.json()
                if data.get("status") in _TERMINAL_STATUSES:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Market research job {job_id} timed out after {_TOTAL_TIMEOUT_S}s")
                time.sleep(_POLL_INTERVAL_S)
    except (httpx.HTTPError, httpx.TimeoutException, ValueError, KeyError) as e:
        raise RuntimeError(f"Market research request failed: {e}") from e

    if data.get("status") != "completed":
        raise RuntimeError(
            f"Market research job {job_id} ended with status {data.get('status')}: {data.get('error')}"
        )

    result = data.get("result") or {}
    return _map_to_competitive_snapshot(result)


def _map_to_competitive_snapshot(data: dict) -> CompetitiveSnapshot:
    """Map Market Research TeamOutput to CompetitiveSnapshot."""
    summary = data.get("mission_summary", "")
    insights_list = []
    rec = data.get("recommendation") or {}
    if isinstance(rec, dict):
        insights_list.extend(rec.get("rationale", []))
        if rec.get("verdict"):
            summary = summary or rec["verdict"]
    for insight in data.get("insights", []):
        if isinstance(insight, dict) and insight.get("pain_points"):
            insights_list.extend(insight["pain_points"])
    similar_brands = []
    for sig in data.get("market_signals", []):
        if isinstance(sig, dict) and sig.get("signal"):
            similar_brands.append(sig["signal"])
    return CompetitiveSnapshot(
        summary=summary[:2000] if summary else "Competitive context requested.",
        similar_brands=similar_brands[:20],
        insights=insights_list[:30],
        source="market_research_team",
    )
