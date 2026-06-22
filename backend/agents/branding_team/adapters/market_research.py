"""Adapter to call the Market Research team API for competitive/similar-brands research."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
from typing import Awaitable, Optional, TypeVar

import httpx

from branding_team.config import env_float
from branding_team.models import BrandingMission, CompetitiveSnapshot

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

_T = TypeVar("_T")


def _run_blocking(coro: Awaitable[_T]) -> _T:
    """Run an awaitable to completion from synchronous code.

    Uses ``asyncio.run`` when no loop is running in this thread; otherwise
    runs it on a one-off worker thread so we never call ``asyncio.run`` inside
    an active loop.

    Preconditions:
        ``coro`` is an un-awaited awaitable/coroutine.
    Postconditions:
        Returns the awaitable's result, or propagates whatever it raises.
        Does not call ``asyncio.run`` while a loop is already running in the
        calling thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()  # type: ignore[arg-type]


def _poll_interval_s() -> float:
    return env_float("BRANDING_MR_POLL_INTERVAL_S", 2.0, positive=True)


def _total_timeout_s() -> float:
    return env_float("BRANDING_MR_TOTAL_TIMEOUT_S", 600.0, positive=True)


def _request_timeout_s() -> float:
    return env_float("BRANDING_MR_REQUEST_TIMEOUT_S", 30.0, positive=True)


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
    root = f"{base.rstrip('/')}/api/market-research"
    payload = _build_payload(mission)
    request_timeout = _request_timeout_s()
    total_timeout = _total_timeout_s()
    poll_interval = _poll_interval_s()
    try:
        async with httpx.AsyncClient() as client:
            submit = await client.post(
                f"{root}/market-research/run", json=payload, timeout=request_timeout
            )
            submit.raise_for_status()
            job_id = submit.json().get("job_id")
            if not job_id:
                raise RuntimeError("Market research submit returned no job_id")

            deadline = time.monotonic() + total_timeout
            while True:
                status = await client.get(
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
                await asyncio.sleep(poll_interval)
    except (httpx.HTTPError, httpx.TimeoutException, ValueError, KeyError) as e:
        raise RuntimeError(f"Market research request failed: {e}") from e

    if data.get("status") != "completed":
        raise RuntimeError(
            f"Market research job {job_id} ended with status {data.get('status')}: {data.get('error')}"
        )

    result = data.get("result") or {}
    return _map_to_competitive_snapshot(result)


def request_market_research(mission: BrandingMission) -> Optional[CompetitiveSnapshot]:
    """Synchronous wrapper over :func:`request_market_research_async` for
    non-async callers (e.g. the orchestrator running in a worker thread)."""
    return _run_blocking(request_market_research_async(mission))


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
