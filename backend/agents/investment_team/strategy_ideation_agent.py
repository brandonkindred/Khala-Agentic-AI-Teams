"""
Strategy Ideation Agent — generates swing trading strategies using LLM, informed by past results.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from llm_service.interface import LLMClient

from .models import StrategyLabRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_IDEATION_SYSTEM = (
    "You are an expert quantitative trading strategy designer specializing in swing trading. "
    "Your role is to generate novel, testable trading strategies for stocks and cryptocurrency markets. "
    "Focus on strategies that could realistically achieve 8%+ annualized returns through systematic rules."
)

_IDEATION_PROMPT = """\
Generate a novel swing trading strategy for stocks or cryptocurrency.

Swing trading targets holding periods of 2–14 days, capturing short-term price swings.
Goal: exceed 8% annualized return with controlled drawdown.

## Prior Strategy Results (last {n_prior} tested)
{prior_results_text}

## Instructions
Generate a strategy that DIFFERS from the prior strategies above — explore under-tested approaches.
Return ONLY a JSON object with no markdown:
{{
  "asset_class": "stocks" or "crypto",
  "hypothesis": "1-2 sentence investment thesis",
  "signal_definition": "specific technical or fundamental signal description",
  "entry_rules": ["entry rule 1", "entry rule 2", "entry rule 3"],
  "exit_rules": ["exit rule 1", "exit rule 2"],
  "sizing_rules": ["sizing rule 1"],
  "risk_limits": {{"max_position_pct": 5, "stop_loss_pct": 3}},
  "speculative": false,
  "rationale": "why you chose this strategy given prior results"
}}
"""

_ANALYSIS_SYSTEM = "You are a quantitative trading analyst. Write concise, insightful backtest analysis narratives."

_ANALYSIS_PROMPT = """\
Analyze the following swing trading strategy backtest.

## Strategy
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Rationale for testing: {rationale}

## Backtest Results
Annualized return: {annualized_return_pct:.1f}%
Total return: {total_return_pct:.1f}%
Sharpe ratio: {sharpe_ratio:.2f}
Max drawdown: {max_drawdown_pct:.1f}%
Win rate: {win_rate_pct:.1f}%
Profit factor: {profit_factor:.2f}
Volatility: {volatility_pct:.1f}%
Outcome: {outcome_label}

## Instructions
Write a 3-5 sentence analytical narrative covering:
1. What strategy was tested and why it was chosen
2. What the results reveal about the strategy's edge (or lack thereof)
3. The likely reason for its success or failure

Return ONLY a JSON object with no markdown:
{{"narrative": "your analysis here"}}
"""


def _format_prior_results(records: List[StrategyLabRecord]) -> str:
    if not records:
        return "None yet — this is the first strategy."
    lines = []
    for r in records[-10:]:  # show last 10 at most
        label = "WINNING" if r.is_winning else "LOSING"
        lines.append(
            f"- [{label}] {r.strategy.asset_class} | {r.strategy.hypothesis[:80]} "
            f"| Annual: {r.backtest.result.annualized_return_pct:.1f}%"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class StrategyIdeationAgent:
    """
    Uses an LLM to:
      1. Ideate a novel swing trading strategy informed by past results.
      2. Generate a post-backtest narrative explaining success or failure.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm = llm_client

    def ideate_strategy(
        self,
        prior_results: Optional[List[StrategyLabRecord]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Generate a new strategy spec dict and rationale.

        Returns:
            (strategy_dict, rationale) — strategy_dict matches StrategySpec fields,
            rationale is the LLM's explanation for the choice.
        """
        prior_results = prior_results or []
        prior_text = _format_prior_results(prior_results)

        prompt = _IDEATION_PROMPT.format(
            n_prior=len(prior_results),
            prior_results_text=prior_text,
        )

        data = self.llm.complete_json(
            prompt,
            temperature=0.8,
            system_prompt=_IDEATION_SYSTEM,
            think=True,
        )

        rationale = str(data.pop("rationale", "No rationale provided."))
        return data, rationale

    def analyze_result(
        self,
        record: StrategyLabRecord,
        rationale: str,
    ) -> str:
        """
        Generate a narrative analysis of a completed backtest.

        Returns:
            A plain-text narrative string.
        """
        result = record.backtest.result
        strategy = record.strategy
        outcome_label = (
            "WINNING (>8% annualized)" if record.is_winning else "LOSING (<8% annualized)"
        )

        prompt = _ANALYSIS_PROMPT.format(
            asset_class=strategy.asset_class,
            hypothesis=strategy.hypothesis,
            signal_definition=strategy.signal_definition,
            entry_rules="; ".join(strategy.entry_rules),
            exit_rules="; ".join(strategy.exit_rules),
            rationale=rationale,
            annualized_return_pct=result.annualized_return_pct,
            total_return_pct=result.total_return_pct,
            sharpe_ratio=result.sharpe_ratio,
            max_drawdown_pct=result.max_drawdown_pct,
            win_rate_pct=result.win_rate_pct,
            profit_factor=result.profit_factor,
            volatility_pct=result.volatility_pct,
            outcome_label=outcome_label,
        )

        data = self.llm.complete_json(
            prompt,
            temperature=0.3,
            system_prompt=_ANALYSIS_SYSTEM,
            think=True,
        )
        return str(data.get("narrative", "Analysis not available."))
