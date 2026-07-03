"""SE team API — operational routes: supervisor logs, health, and DORA metrics."""

import logging
import os
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from software_engineering_team.api import main as _main
from software_engineering_team.api.state import (
    ALLOWED_SERVICES,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/logs", response_class=PlainTextResponse)
def get_logs(
    service: str = "sw_api",
    lines: int = 500,
    stderr: bool = False,
) -> PlainTextResponse:
    """
    Return recent supervisor log content for debugging (only when ENABLE_LOG_API=1).
    Query params: service (e.g. sw_api, blogging_api, or 'all'), lines (default 500), stderr (include *_err.log).
    """
    if os.environ.get("ENABLE_LOG_API", "").strip() not in ("1", "true", "True"):
        raise HTTPException(status_code=404, detail="Log API disabled")
    if not _main.SUPERVISOR_LOG_DIR.exists():
        raise HTTPException(status_code=503, detail="Log directory not available")
    if service != "all" and service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=400, detail=f"Unknown service. Allowed: {sorted(ALLOWED_SERVICES)} or 'all'"
        )
    lines = max(1, min(lines, 10000))
    parts: List[str] = []
    if service == "all":
        candidates = sorted(ALLOWED_SERVICES - {"postgresql", "dockerd"})
    else:
        candidates = [service]
    for name in candidates:
        for suffix in (".log", "_err.log") if stderr else (".log",):
            path = _main.SUPERVISOR_LOG_DIR / f"{name}{suffix}"
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    tail = "\n".join(content.splitlines()[-lines:])
                    parts.append(f"=== {path.name} ===\n{tail}")
                except OSError as e:
                    parts.append(f"=== {path.name} (read error: {e}) ===\n")
    if not parts:
        return PlainTextResponse(content="(no log files found)\n", status_code=200)
    return PlainTextResponse(content="\n\n".join(parts))


@router.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/dora")
def metrics_dora(window_days: float = 30.0) -> dict:
    """DORA metrics + cost over the last ``window_days`` (clamped to [1, 365]).

    Reachable through the unified proxy at ``/api/software-engineering/dora`` (and
    the ``/api/se/metrics`` alias). The path deliberately avoids ``metrics`` so it
    is not swept into the OTel ``excluded_urls`` filter (which excludes scrape
    endpoints named ``metrics``) — this business endpoint stays traced. Returns
    all-zero metrics when Postgres is disabled rather than erroring, so the UI can
    render a "no data" state.
    """
    window = max(1.0, min(365.0, window_days))
    try:
        # Import inside the try so even a packaging/circular-import failure of the
        # metrics module degrades to the zeroed "no data" response below rather
        # than surfacing a 500 — the endpoint's contract is to always return a
        # valid metrics shape.
        from software_engineering_team.metrics.dora import compute_dora

        return compute_dora(window).to_dict()
    except Exception:
        logger.exception("failed to compute DORA metrics")
        # Build the zeroed fallback as a literal (not via DoraMetrics) so it holds
        # even when the metrics module itself cannot be imported. Mirrors
        # DoraMetrics' field defaults; keep in sync with that dataclass.
        return {
            "window_days": window,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "deployment_count": 0,
            "deployment_frequency_per_day": 0.0,
            "lead_time_seconds_median": None,
            "lead_time_sample_count": 0,
            "merged_count": 0,
            "gate_reentry_count": 0,
            "change_failure_rate": 0.0,
            "mttr_seconds_median": None,
            "crash_resolved_count": 0,
            "total_cost_usd": 0.0,
            "cost_by_job": {},
        }
