"""
Agent performance analytics API.

Endpoints:
- GET /api/analytics/team/{team}/scorecard  — team quality scorecard
- GET /api/analytics/signals                — raw signal history
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

_agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from analytics.signals import get_signals, get_team_scorecard  # noqa: E402

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/team/{team}/scorecard")
def team_scorecard(
    team: str,
    window: float = Query(24.0, description="Time window in hours"),
) -> dict[str, Any]:
    """Quality scorecard for a team: pass rates, retry counts, build success, etc."""
    return get_team_scorecard(team, window_hours=window)


@router.get("/signals")
def signal_history(
    team: str | None = Query(None),
    signal_type: str | None = Query(None),
    window: float = Query(24.0),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Raw quality signal events, filterable by team and type."""
    from analytics.signals import SignalType

    st = None
    if signal_type:
        with contextlib.suppress(ValueError):
            st = SignalType(signal_type)
    return get_signals(team=team, signal_type=st, window_hours=window, limit=limit)
