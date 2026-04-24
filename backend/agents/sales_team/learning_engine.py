"""LearningEngine — analyzes accumulated sales outcomes and extracts actionable
patterns to improve the pipeline.

The engine is called on demand (via POST /sales/insights/refresh) or
automatically after every N deal outcomes are recorded. It:

1. Loads all StageOutcome and DealOutcome records from the outcome store.
2. Passes them to the shared ``llm_service`` with a specialized analysis prompt.
3. Validates the response directly against :class:`LearningInsights`.
4. Persists the result to the outcome store so all agents can read it on the
   next pipeline run.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic import BaseModel, Field

from llm_service import LLMClient, complete_validated

from .llm import get_sales_llm_client
from .models import DealOutcome, LearningInsights, StageOutcome
from .outcome_store import (
    load_current_insights,
    load_deal_outcomes,
    load_stage_outcomes,
    save_insights,
)

logger = logging.getLogger(__name__)


class _LearningInsightsBody(BaseModel):
    """LLM response schema for the learning engine.

    Excludes the stamp fields ``generated_at`` and ``insights_version`` which
    the engine assigns after validation (they track persistence state, not
    model output).
    """

    total_outcomes_analyzed: int = 0
    win_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    stage_conversion_rates: dict = Field(default_factory=dict)
    top_performing_industries: List[str] = Field(default_factory=list)
    top_icp_signals: List[str] = Field(default_factory=list)
    best_outreach_angles: List[str] = Field(default_factory=list)
    common_objections: List[str] = Field(default_factory=list)
    best_close_techniques: List[str] = Field(default_factory=list)
    winning_patterns: List[str] = Field(default_factory=list)
    losing_patterns: List[str] = Field(default_factory=list)
    avg_deal_size_won_usd: Optional[float] = None
    avg_sales_cycle_days: Optional[float] = None
    actionable_recommendations: List[str] = Field(default_factory=list)


_LEARNING_SYSTEM_PROMPT = """You are a Sales Analytics Expert who analyzes historical sales pipeline data
to extract patterns that help sales teams improve their win rates and process efficiency.

## Your Analytical Framework

### Win/Loss Pattern Analysis (Gong Labs)
Identify which behaviors, deal traits, and timing patterns correlate with wins vs. losses:
- Multi-threading (multiple contacts engaged) → higher win rate
- Champion present + EB on a call → 3× win rate
- Proposal sent within 24h of discovery → faster cycle
- No next step booked → 80% stall rate

### ICP Signal Evaluation (Jeb Blount / Sales Hacker)
Score which firmographic traits predicted deal success:
- Industries with win rate > 50% are tier-1 ICP targets
- ICP match score thresholds that correlated with closed-won deals
- Trigger events (funding, hiring, leadership change) that predicted faster cycles

### Outreach Effectiveness (Salesfolk / Gong Labs)
Identify which outreach patterns drove replies and meetings:
- Which email touch number got the most replies (Gong: #2 or #3 most common)
- Subject line patterns with high open/reply correlation
- Channels that drove conversion at each stage

### Qualification Accuracy (Anthony Iannarino / MEDDIC)
Assess whether the qualification signals were reliable predictors:
- Did high BANT scores reliably predict wins?
- Which MEDDIC signals were present in won deals but absent in lost deals?
- What was the average qualification score for won vs. lost deals?

### Objection Intelligence (Zig Ziglar / Jeb Blount)
Map objections to outcomes:
- Which objections were successfully overcome (led to close)?
- Which objections were consistent loss predictors?
- Which close techniques had highest win rates?

### Pipeline Velocity (HubSpot)
Identify velocity bottlenecks:
- Average stage duration for won vs. lost deals
- Which stage had the lowest conversion rate (biggest leak in the funnel)?
- How does sales cycle length correlate with deal size?

## Output Requirements
Return a JSON object with exactly these keys:
{
  "total_outcomes_analyzed": <int>,
  "win_rate": <float 0-1>,
  "stage_conversion_rates": {"stage_name": <float>, ...},
  "top_performing_industries": [<string>, ...],
  "top_icp_signals": [<string>, ...],
  "best_outreach_angles": [<string>, ...],
  "common_objections": [<string>, ...],
  "best_close_techniques": [<string>, ...],
  "winning_patterns": [<string>, ...],
  "losing_patterns": [<string>, ...],
  "avg_deal_size_won_usd": <float or null>,
  "avg_sales_cycle_days": <float or null>,
  "actionable_recommendations": [<string>, ...]
}

Each string in arrays should be a specific, actionable insight — not generic advice.
Recommendations must reference specific numbers from the data when available.
"""


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class LearningEngine:
    """Analyzes accumulated outcomes and refreshes LearningInsights.

    Call ``.refresh()`` to run the analysis and persist updated insights.
    """

    llm_client: Optional[LLMClient] = None
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("learning_engine")

    def refresh(
        self,
        stage_outcomes: Optional[List[StageOutcome]] = None,
        deal_outcomes: Optional[List[DealOutcome]] = None,
    ) -> LearningInsights:
        """Analyze outcomes and return (and persist) updated LearningInsights.

        If stage_outcomes / deal_outcomes are not provided, they are loaded
        from the outcome store automatically.
        """
        if stage_outcomes is None:
            stage_outcomes = load_stage_outcomes()
        if deal_outcomes is None:
            deal_outcomes = load_deal_outcomes()

        current = load_current_insights()
        current_version = current.insights_version if current else 0
        n_analyzed = len(stage_outcomes) + len(deal_outcomes)

        if n_analyzed == 0:
            logger.info("LearningEngine: no outcomes to analyze yet — returning empty insights")
            empty = LearningInsights(
                total_outcomes_analyzed=0,
                actionable_recommendations=[
                    "No outcomes recorded yet. Use POST /sales/outcomes/stage or "
                    "POST /sales/outcomes/deal to log results as you work deals."
                ],
                generated_at=_utc_now_iso(),
                insights_version=current_version + 1,
            )
            save_insights(empty)
            return empty

        body = self._generate_insights(stage_outcomes, deal_outcomes)
        insights = LearningInsights(
            **body.model_dump(),
            generated_at=_utc_now_iso(),
            insights_version=current_version + 1,
        )
        # Defensive: if the LLM under-reported total_outcomes, fall back to our
        # actual count so the UI shows the right number.
        if insights.total_outcomes_analyzed == 0:
            insights.total_outcomes_analyzed = n_analyzed

        save_insights(insights)
        logger.info(
            "LearningEngine: insights refreshed to v%d — win_rate=%.0f%%, %d outcomes",
            insights.insights_version,
            insights.win_rate * 100,
            insights.total_outcomes_analyzed,
        )
        return insights

    def _generate_insights(
        self,
        stage_outcomes: List[StageOutcome],
        deal_outcomes: List[DealOutcome],
    ) -> _LearningInsightsBody:
        stage_data = [s.model_dump() for s in stage_outcomes]
        deal_data = [d.model_dump() for d in deal_outcomes]
        prompt = (
            f"Analyze these sales pipeline outcomes and extract actionable patterns.\n\n"
            f"STAGE OUTCOMES ({len(stage_outcomes)} records):\n"
            f"{json.dumps(stage_data, indent=2)}\n\n"
            f"DEAL OUTCOMES ({len(deal_outcomes)} records):\n"
            f"{json.dumps(deal_data, indent=2)}\n\n"
            "Return a single JSON object with the insights schema defined in your system prompt. "
            "All insights must be grounded in the specific data above — no generic advice."
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=_LearningInsightsBody,
            system_prompt=_LEARNING_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


def format_insights_for_prompt(insights: Optional[LearningInsights]) -> str:
    """Format LearningInsights as a concise block for injection into agent prompts.

    Returns an empty string if insights is None or has no analyzed outcomes.
    """
    if not insights or insights.total_outcomes_analyzed == 0:
        return ""

    lines = [
        f"\n## Learned from {insights.total_outcomes_analyzed} past outcomes "
        f"(win rate: {insights.win_rate:.0%}, insights v{insights.insights_version})\n"
    ]

    if insights.winning_patterns:
        lines.append("**What's working:**")
        for p in insights.winning_patterns[:3]:
            lines.append(f"- {p}")

    if insights.losing_patterns:
        lines.append("\n**Watch out for:**")
        for p in insights.losing_patterns[:3]:
            lines.append(f"- {p}")

    if insights.top_performing_industries:
        lines.append(f"\n**Top industries:** {', '.join(insights.top_performing_industries)}")

    if insights.common_objections:
        lines.append("\n**Most frequent objections to prepare for:**")
        for o in insights.common_objections[:3]:
            lines.append(f"- {o}")

    if insights.best_close_techniques:
        lines.append(
            f"\n**Best close techniques (by win rate):** {', '.join(insights.best_close_techniques)}"
        )

    if insights.best_outreach_angles:
        lines.append("\n**High-reply outreach angles:**")
        for a in insights.best_outreach_angles[:2]:
            lines.append(f"- {a}")

    if insights.actionable_recommendations:
        lines.append("\n**Top recommendations:**")
        for r in insights.actionable_recommendations[:3]:
            lines.append(f"- {r}")

    if insights.avg_sales_cycle_days:
        lines.append(f"\n**Avg sales cycle (won):** {insights.avg_sales_cycle_days:.0f} days")

    if insights.stage_conversion_rates:
        worst = min(
            insights.stage_conversion_rates, key=lambda k: insights.stage_conversion_rates[k]
        )
        lines.append(
            f"**Biggest funnel leak:** {worst} stage "
            f"({insights.stage_conversion_rates[worst]:.0%} conversion)"
        )

    return "\n".join(lines)
