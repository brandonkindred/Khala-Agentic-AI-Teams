"""
LLM usage telemetry API — token consumption, cost attribution, and call history.

Endpoints:
- GET /api/llm-usage           — aggregated usage summary (filterable by team, time window)
- GET /api/llm-usage/recent    — recent individual LLM call records
- GET /api/llm-usage/health    — circuit breaker states for all teams
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

# Ensure agents directory is importable (same pattern as main.py)
_agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from llm_service.telemetry import get_recent_calls, get_usage_summary  # noqa: E402

router = APIRouter(prefix="/api/llm-usage", tags=["llm-usage"])


@router.get("/")
def usage_summary(
    team: str | None = Query(None, description="Filter by team name"),
    window: float = Query(24.0, description="Time window in hours"),
) -> dict[str, Any]:
    """Aggregated LLM token usage over a time window.

    Returns total calls, token counts (prompt/completion/total), average latency,
    error count, and breakdowns by agent and model.
    """
    return get_usage_summary(team=team, window_hours=window)


@router.get("/recent")
def recent_calls(
    team: str | None = Query(None, description="Filter by team name"),
    limit: int = Query(100, ge=1, le=1000, description="Max records to return"),
) -> list:
    """Recent individual LLM call records (most recent last)."""
    return get_recent_calls(team=team, limit=limit)


@router.get("/health")
def proxy_health() -> dict[str, Any]:
    """Circuit breaker states for all proxied teams."""
    from unified_api.team_proxy import circuit_breaker

    return {
        "circuit_breakers": circuit_breaker.get_all_states(),
    }
