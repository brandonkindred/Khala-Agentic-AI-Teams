"""Adapter to call the Market Research team API for competitive/similar-brands research."""

from __future__ import annotations

import os
from typing import Optional

from branding_team.models import BrandingMission, CompetitiveSnapshot
from branding_team.shared.coro_runner import run_coroutine
from shared.env_config import env_float
from shared.http.job_polling import (
    DEFAULT_TERMINAL_STATUSES,
    async_get_json,
    async_poll_until_terminal,
    async_post_json,
)


def _poll_interval_s() -> float:
    return env_float("BRANDING_MR_POLL_INTERVAL_S", 2.0, floor=0.1)


def _total_timeout_s() -> float:
    return env_float("BRANDING_MR_TOTAL_TIMEOUT_S", 600.0, floor=1.0)


def _request_timeout_s() -> float:
    return env_float("BRANDING_MR_REQUEST_TIMEOUT_S", 30.0, floor=1.0)


def _base_url() -> Optional[str]:
    return os.environ.get("UNIFIED_API_BASE_URL") or os.environ.get("BRANDING_MARKET_RESEARCH_URL")


def _build_payload(mission: BrandingMission) -> dict:
    """Build the Market Research ``/run`` request body from a mission.

    Preconditions:
        ``mission`` has at least ``company_name``, ``company_description`` and
        ``target_audience`` populated.
    Postconditions:
        Returns a JSON-serialisable dict with the keys the market-research
        ``run`` endpoint expects (product_concept, target_users, business_goal,
        human_approved, human_feedback).
    """
    product_concept = (
        f"Competitive and similar brands for {mission.company_name}: {mission.company_description}"
    )
    differentiators = (
        ", ".join(mission.differentiators) if mission.differentiators else "differentiate"
    )
    business_goal = f"Differentiate and position brand. Key differentiators: {differentiators}"
    return {
        "product_concept": product_concept,
        "target_users": mission.target_audience,
        "business_goal": business_goal,
        "human_approved": True,
        "human_feedback": "Branding team requested competitive snapshot.",
    }


async def request_market_research_async(
    mission: BrandingMission,
) -> Optional[CompetitiveSnapshot]:
    """Async variant: submit a job and poll with ``asyncio.sleep``.

    The poll yields to the event loop between attempts, so an awaiting caller
    (e.g. an async endpoint) does not hold a worker thread for the lifetime of
    a multi-minute job.

    Preconditions:
        ``mission`` carries the fields ``_build_payload`` requires.
    Postconditions:
        Returns a CompetitiveSnapshot on success, or None when the service is
        unconfigured (neither base-URL env var set). Raises RuntimeError on
        transport/parse errors, a missing job id, timeout, or terminal job
        failure.
    """
    base = _base_url()
    if not base:
        return None
    # The doubled ``market-research`` segment below is intentional, not a typo:
    # the Unified API mounts the Market Research team's app under the prefix
    # ``/api/market-research`` (unified_api/config.py), and that app's own routes
    # are ``/market-research/run`` and ``/market-research/status/{job_id}``
    # (market_research_team/api/main.py). The full live path is therefore
    # ``/api/market-research/market-research/run``.
    root = f"{base.rstrip('/')}/api/market-research"
    payload = _build_payload(mission)
    request_timeout = _request_timeout_s()
    total_timeout = _total_timeout_s()
    poll_interval = _poll_interval_s()
    submitted = await async_post_json(
        f"{root}/market-research/run",
        payload,
        timeout=request_timeout,
        log_context="Market research submit",
    )
    if submitted is None:
        raise RuntimeError("Market research request failed: submit request failed")
    job_id = submitted.get("job_id")
    if not job_id:
        raise RuntimeError("Market research submit returned no job_id")

    data = await async_poll_until_terminal(
        lambda: async_get_json(
            f"{root}/market-research/status/{job_id}",
            timeout=request_timeout,
            log_context=f"Market research status for {job_id}",
        ),
        terminal_statuses=DEFAULT_TERMINAL_STATUSES,
        poll_interval=poll_interval,
        total_timeout=total_timeout,
        log_context=f"market research job {job_id}",
    )

    if data.get("status") != "completed":
        raise RuntimeError(
            f"Market research job {job_id} ended with status {data.get('status')}: {data.get('error')}"
        )

    result = data.get("result") or {}
    return _map_to_competitive_snapshot(result)


def request_market_research(mission: BrandingMission) -> Optional[CompetitiveSnapshot]:
    """Synchronous wrapper over :func:`request_market_research_async` for
    non-async callers (e.g. the orchestrator running in a worker thread)."""
    return run_coroutine(request_market_research_async(mission))


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
        summary=summary if summary else "Competitive context requested.",
        similar_brands=similar_brands[:20],
        insights=insights_list[:30],
        source="market_research_team",
    )
