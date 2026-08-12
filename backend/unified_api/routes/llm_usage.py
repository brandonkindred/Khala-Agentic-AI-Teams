"""
LLM usage telemetry API — token consumption and call history.

Endpoints:
- GET /api/llm-usage           — aggregated usage summary (filterable by team, time window)
- GET /api/llm-usage/recent    — recent individual LLM call records
- GET /api/llm-usage/health    — circuit breaker states for all teams
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

_agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from llm_service.telemetry import get_recent_calls, get_usage_summary  # noqa: E402
from llm_service.usage_store import fetch_recent, fetch_summary, window_hours  # noqa: E402
from shared.postgres import is_postgres_enabled, resolve_storage_status  # noqa: E402

router = APIRouter(prefix="/api/llm-usage", tags=["llm-usage"])

WindowPreset = Literal["24h", "7d", "30d", "all"]


def _attach_storage(data: dict[str, Any]) -> dict[str, Any]:
    status = resolve_storage_status()
    data["storage_status"] = status
    data["storage_available"] = status == "available"
    return data


@router.get("/")
def usage_summary(
    team: Annotated[str | None, Query(description="Filter by team name")] = None,
    window: Annotated[WindowPreset, Query(description="Time window preset")] = "24h",
) -> dict[str, Any]:
    """Aggregated LLM token usage over a preset window."""
    if is_postgres_enabled():
        data = fetch_summary(window=window, team=team)
    else:
        hours = window_hours(window)
        data = get_usage_summary(team=team, window_hours=hours)
        data["window"] = window
        data["window_hours"] = hours
    return _attach_storage(data)


@router.get("/recent")
def recent_calls(
    team: Annotated[str | None, Query(description="Filter by team name")] = None,
    window: Annotated[WindowPreset, Query(description="Time window preset")] = "24h",
    limit: Annotated[int, Query(ge=1, le=1000, description="Max records to return")] = 100,
) -> list:
    """Recent individual LLM call records, newest first."""
    if is_postgres_enabled():
        return fetch_recent(window=window, team=team, limit=limit)
    hours = window_hours(window)
    cutoff = None if hours <= 0 else time.time() - hours * 3600
    records = get_recent_calls(team=team, limit=1000)
    if cutoff is not None:
        records = [r for r in records if r.get("timestamp", 0) >= cutoff]
    records.reverse()  # get_recent_calls is oldest→newest; API is newest first
    return records[:limit]


@router.get("/health")
def proxy_health() -> dict[str, Any]:
    """Circuit breaker states for all proxied teams."""
    from unified_api.team_proxy import circuit_breaker

    return {"circuit_breakers": circuit_breaker.get_all_states()}
