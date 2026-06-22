"""Adapter to call the Market Research team API for competitive/similar-brands research."""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from branding_team.models import BrandingMission, CompetitiveSnapshot

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _float_env(name: str, default: float) -> float:
    """Parse a positive float env var, falling back to *default* on garbage."""
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _poll_interval_s() -> float:
    return _float_env("BRANDING_MR_POLL_INTERVAL_S", 2.0)


def _total_timeout_s() -> float:
    return _float_env("BRANDING_MR_TOTAL_TIMEOUT_S", 600.0)


def _request_timeout_s() -> float:
    return _float_env("BRANDING_MR_REQUEST_TIMEOUT_S", 30.0)


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
    request_timeout = _request_timeout_s()
    total_timeout = _total_timeout_s()
    poll_interval = _poll_interval_s()
    # A fresh client per call: httpx.Client is not safe to share across the
    # branding-run worker threads, and one client still serves this job's
    # submit + all its status polls. The connection is reused within the job
    # and closed when it finishes.
    try:
        with httpx.Client() as client:
            submit = client.post(
                f"{root}/market-research/run", json=payload, timeout=request_timeout
            )
            submit.raise_for_status()
            job_id = submit.json().get("job_id")
            if not job_id:
                raise RuntimeError("Market research submit returned no job_id")

            deadline = time.monotonic() + total_timeout
            while True:
                status = client.get(
                    f"{root}/market-research/status/{job_id}", timeout=request_timeout
                )
                status.raise_for_status()
                data = status.json()
                if data.get("status") in _TERMINAL_STATUSES:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Market research job {job_id} timed out after {total_timeout}s"
                    )
                time.sleep(poll_interval)
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
