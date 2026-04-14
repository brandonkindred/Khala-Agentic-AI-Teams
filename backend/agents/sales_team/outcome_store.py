"""Persistent file-backed store for sales outcome records and learned insights.

Outcomes are written as individual JSON files under:
  .agent_cache/sales_team/outcomes/stage/<id>.json
  .agent_cache/sales_team/outcomes/deal/<id>.json

The current LearningInsights snapshot is stored at:
  .agent_cache/sales_team/insights/current.json

Thread-safe via a single module-level lock (same process) and atomic
file-writes (write to tmp then rename) for cross-process safety.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import DealOutcome, LearningInsights, StageOutcome

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE_ROOT = Path(os.getenv("AGENT_CACHE_DIR", ".agent_cache")) / "sales_team" / "outcomes"
_INSIGHTS_PATH = (
    Path(os.getenv("AGENT_CACHE_DIR", ".agent_cache")) / "sales_team" / "insights" / "current.json"
)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: write to .tmp then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


def record_stage_outcome(outcome: StageOutcome) -> StageOutcome:
    """Persist a stage outcome and return it with outcome_id and recorded_at set."""
    with _LOCK:
        oid = outcome.outcome_id or str(uuid.uuid4())
        ts = outcome.recorded_at or _now()
        filled = outcome.model_copy(update={"outcome_id": oid, "recorded_at": ts})
        path = _CACHE_ROOT / "stage" / f"{oid}.json"
        _atomic_write(path, filled.model_dump())
        logger.debug(
            "Recorded stage outcome %s for %s @ %s", oid, outcome.company_name, outcome.stage
        )
        return filled


def record_deal_outcome(outcome: DealOutcome) -> DealOutcome:
    """Persist a deal outcome and return it with outcome_id and recorded_at set."""
    with _LOCK:
        oid = outcome.outcome_id or str(uuid.uuid4())
        ts = outcome.recorded_at or _now()
        filled = outcome.model_copy(update={"outcome_id": oid, "recorded_at": ts})
        path = _CACHE_ROOT / "deal" / f"{oid}.json"
        _atomic_write(path, filled.model_dump())
        logger.debug(
            "Recorded deal outcome %s for %s: %s", oid, outcome.company_name, outcome.result
        )
        return filled


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def load_stage_outcomes(limit: int = 500) -> List[StageOutcome]:
    """Return up to *limit* stage outcomes, newest first."""
    stage_dir = _CACHE_ROOT / "stage"
    if not stage_dir.exists():
        return []
    files = sorted(stage_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    results = []
    for f in files:
        data = _read_json(f)
        if data:
            try:
                results.append(StageOutcome(**data))
            except Exception as exc:
                logger.warning("Corrupt stage outcome file %s: %s", f, exc)
    return results


def load_deal_outcomes(limit: int = 500) -> List[DealOutcome]:
    """Return up to *limit* deal outcomes, newest first."""
    deal_dir = _CACHE_ROOT / "deal"
    if not deal_dir.exists():
        return []
    files = sorted(deal_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    results = []
    for f in files:
        data = _read_json(f)
        if data:
            try:
                results.append(DealOutcome(**data))
            except Exception as exc:
                logger.warning("Corrupt deal outcome file %s: %s", f, exc)
    return results


def load_current_insights() -> Optional[LearningInsights]:
    """Return the latest persisted LearningInsights, or None if never generated."""
    data = _read_json(_INSIGHTS_PATH)
    if not data:
        return None
    try:
        return LearningInsights(**data)
    except Exception as exc:
        logger.warning("Could not parse insights: %s", exc)
        return None


def save_insights(insights: LearningInsights) -> None:
    """Persist a refreshed LearningInsights snapshot."""
    with _LOCK:
        _atomic_write(_INSIGHTS_PATH, insights.model_dump())
        logger.info(
            "Saved learning insights v%d (%d outcomes)",
            insights.insights_version,
            insights.total_outcomes_analyzed,
        )


def outcome_counts() -> dict:
    """Return a quick summary dict (no heavy parsing)."""
    stage_dir = _CACHE_ROOT / "stage"
    deal_dir = _CACHE_ROOT / "deal"
    return {
        "stage_outcomes": len(list(stage_dir.glob("*.json"))) if stage_dir.exists() else 0,
        "deal_outcomes": len(list(deal_dir.glob("*.json"))) if deal_dir.exists() else 0,
        "has_insights": _INSIGHTS_PATH.exists(),
    }


