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


# ---------------------------------------------------------------------------
# Heuristic fallback — compute basic insights without an LLM
# ---------------------------------------------------------------------------


def compute_heuristic_insights(
    stage_outcomes: List[StageOutcome],
    deal_outcomes: List[DealOutcome],
    current_version: int = 0,
) -> LearningInsights:
    """Derive LearningInsights from raw outcome data using pure heuristics.

    This runs when the Strands SDK is unavailable, ensuring the learning loop
    always produces *something* useful.
    """
    from collections import Counter

    total = len(deal_outcomes)
    won = [d for d in deal_outcomes if d.result.value == "won"]
    lost = [d for d in deal_outcomes if d.result.value == "lost"]
    win_rate = round(len(won) / total, 3) if total else 0.0

    # Top industries (by win count)
    industry_wins: Counter[str] = Counter()
    for d in won:
        if d.industry:
            industry_wins[d.industry] += 1
    top_industries = [i for i, _ in industry_wins.most_common(3)]

    # Common objections across all stage outcomes + deal outcomes
    obj_counter: Counter[str] = Counter()
    for s in stage_outcomes:
        if s.objection_text:
            obj_counter[s.objection_text] += 1
    for d in deal_outcomes:
        for obj in d.objections_raised:
            obj_counter[obj] += 1
    common_objections = [o for o, _ in obj_counter.most_common(5)]

    # Best close techniques (by win association)
    close_wins: Counter[str] = Counter()
    for d in won:
        if d.close_technique_used:
            close_wins[d.close_technique_used.value] += 1
    best_closes = [c for c, _ in close_wins.most_common(3)]

    # Stage conversion rates from stage_outcomes
    stage_counts: Counter[str] = Counter()
    stage_converts: Counter[str] = Counter()
    for s in stage_outcomes:
        stage_counts[s.stage.value] += 1
        if s.outcome.value == "converted":
            stage_converts[s.stage.value] += 1
    conversion_rates = {
        stage: round(stage_converts[stage] / stage_counts[stage], 3) for stage in stage_counts
    }

    # Win / loss patterns
    loss_reasons: Counter[str] = Counter()
    win_factors: Counter[str] = Counter()
    for d in lost:
        if d.loss_reason:
            loss_reasons[d.loss_reason] += 1
    for d in won:
        if d.win_factor:
            win_factors[d.win_factor] += 1
    losing_patterns = [r for r, _ in loss_reasons.most_common(3)]
    winning_patterns = [f for f, _ in win_factors.most_common(3)]

    # Avg deal size and cycle
    won_sizes = [d.deal_size_usd for d in won if d.deal_size_usd]
    avg_deal = round(sum(won_sizes) / len(won_sizes), 2) if won_sizes else None
    won_cycles = [d.sales_cycle_days for d in won if d.sales_cycle_days]
    avg_cycle = round(sum(won_cycles) / len(won_cycles), 1) if won_cycles else None

    # Email subject lines that drove replies
    subject_counter: Counter[str] = Counter()
    for s in stage_outcomes:
        if s.stage.value == "outreach" and s.outcome.value == "converted" and s.subject_line_used:
            subject_counter[s.subject_line_used] += 1
    best_angles = [sl for sl, _ in subject_counter.most_common(3)]

    # ICP signals from won deals
    icp_signals: List[str] = []
    high_score_wins = [d for d in won if d.icp_match_score and d.icp_match_score >= 0.75]
    if high_score_wins:
        icp_signals.append(
            f"ICP match ≥ 0.75 correlated with {len(high_score_wins)}/{len(won)} wins"
        )
    low_score_losses = [d for d in lost if d.icp_match_score and d.icp_match_score < 0.5]
    if low_score_losses:
        icp_signals.append(
            f"ICP match < 0.5 → {len(low_score_losses)}/{len(lost)} losses; tighten ICP filter"
        )

    # Actionable recommendations
    recs: List[str] = []
    if win_rate < 0.3 and total >= 5:
        recs.append(
            f"Win rate is {win_rate:.0%} — review qualification criteria; disqualify low-BANT leads earlier"
        )
    if common_objections:
        recs.append(
            f"Most common objection: '{common_objections[0]}' — prepare a pre-emptive response in proposals"
        )
    if best_closes:
        recs.append(
            f"'{best_closes[0]}' close has highest win association — default to it for similar deals"
        )
    if top_industries:
        recs.append(
            f"Focus prospecting on {', '.join(top_industries)} — highest win rates observed"
        )
    if not recs:
        recs.append(
            f"Pipeline has {total} recorded deal outcomes. "
            "Keep logging outcomes to generate statistically reliable recommendations."
        )

    return LearningInsights(
        total_outcomes_analyzed=len(stage_outcomes) + len(deal_outcomes),
        win_rate=win_rate,
        stage_conversion_rates=conversion_rates,
        top_performing_industries=top_industries,
        top_icp_signals=icp_signals,
        best_outreach_angles=best_angles,
        common_objections=common_objections,
        best_close_techniques=best_closes,
        winning_patterns=winning_patterns,
        losing_patterns=losing_patterns,
        avg_deal_size_won_usd=avg_deal,
        avg_sales_cycle_days=avg_cycle,
        actionable_recommendations=recs,
        generated_at=_now(),
        insights_version=current_version + 1,
    )
