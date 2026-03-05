"""Adapter to call the Market Research team API for competitive/similar-brands research."""

from __future__ import annotations

import os
from typing import Optional

import httpx

from branding_team.models import BrandingMission, CompetitiveSnapshot


def _base_url() -> Optional[str]:
    return os.environ.get("UNIFIED_API_BASE_URL") or os.environ.get("BRANDING_MARKET_RESEARCH_URL")


def request_market_research(mission: BrandingMission) -> Optional[CompetitiveSnapshot]:
    """
    Call the Market Research API with brand context; return CompetitiveSnapshot or None on failure.
    """
    base = _base_url()
    if not base:
        return None
    url = f"{base.rstrip('/')}/api/market-research/market-research/run"
    product_concept = (
        f"Competitive and similar brands for {mission.company_name}: {mission.company_description}"
    )
    target_users = mission.target_audience
    differentiators = ", ".join(mission.differentiators) if mission.differentiators else "differentiate"
    business_goal = f"Differentiate and position brand. Key differentiators: {differentiators}"

    payload = {
        "product_concept": product_concept,
        "target_users": target_users,
        "business_goal": business_goal,
        "human_approved": True,
        "human_feedback": "Branding team requested competitive snapshot.",
    }
    timeout = 120.0
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError, KeyError) as e:
        raise RuntimeError(f"Market research request failed: {e}") from e

    return _map_to_competitive_snapshot(data)


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
