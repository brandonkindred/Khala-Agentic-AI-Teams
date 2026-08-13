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
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

_agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from llm_service.telemetry import get_recent_calls, get_usage_summary  # noqa: E402
from llm_service.usage_store import (  # noqa: E402
    QUERY_FAILED_KEY,
    fetch_recent,
    fetch_summary,
    window_hours,
)
from shared.postgres import is_postgres_enabled, resolve_storage_status  # noqa: E402

router = APIRouter(prefix="/api/llm-usage", tags=["llm-usage"])


def _require_window(window: str) -> str:
    """Accept a preset or numeric-hours window; HTTP 422 otherwise.

    Preconditions: ``window`` is a non-empty str (the raw query value).
    Postconditions: returns ``window`` unchanged when :func:`window_hours`
        accepts it; raises ``HTTPException`` 422 otherwise.
    """
    try:
        window_hours(window)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return window


def _attach_storage(data: dict[str, Any]) -> dict[str, Any]:
    """Stamp storage_status onto a summary body.

    Preconditions: ``data`` is a mutable summary dict from ``fetch_summary`` or
        ``get_usage_summary``.
    Postconditions: ``storage_status`` / ``storage_available`` are set. A usage
        query failure (``QUERY_FAILED_KEY``) with an otherwise-available probe
        is reported as ``unreachable``. The internal marker is removed.
    """
    query_failed = bool(data.pop(QUERY_FAILED_KEY, False))
    status = resolve_storage_status()
    if query_failed and status == "available":
        status = "unreachable"
    data["storage_status"] = status
    data["storage_available"] = status == "available"
    return data


@router.get("/")
def usage_summary(
    team: Annotated[str | None, Query(description="Filter by team name")] = None,
    window: Annotated[str, Query(description="Preset (24h, 7d, 30d, all) or hours (e.g. 1.0)")] = "24h",
) -> dict[str, Any]:
    """Aggregated LLM token usage over a preset or numeric-hours window."""
    _require_window(window)
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
    window: Annotated[str, Query(description="Preset (24h, 7d, 30d, all) or hours (e.g. 1.0)")] = "24h",
    limit: Annotated[int, Query(ge=1, le=1000, description="Max records to return")] = 100,
) -> list:
    """Recent individual LLM call records, oldest-to-newest (most recent last)."""
    _require_window(window)
    if is_postgres_enabled():
        rows = fetch_recent(window=window, team=team, limit=limit)
        if rows is None:
            raise HTTPException(status_code=503, detail="llm usage recent query failed")
        return rows
    hours = window_hours(window)
    cutoff = None if hours <= 0 else time.time() - hours * 3600
    records = get_recent_calls(team=team, limit=1000)
    if cutoff is not None:
        records = [r for r in records if r.get("timestamp", 0) >= cutoff]
    return records[-limit:]


@router.get("/health")
def proxy_health() -> dict[str, Any]:
    """Circuit breaker states for all proxied teams."""
    from unified_api.team_proxy import circuit_breaker

    return {"circuit_breakers": circuit_breaker.get_all_states()}
