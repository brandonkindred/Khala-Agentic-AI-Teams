"""
Strategy Ideation Agent — generates swing trading strategies using LLM, informed by past results.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from strands import Agent

from llm_service import get_strands_model

from .models import StrategyLabRecord, TradeRecord
from .signal_intelligence_agent import brief_to_prompt_block
from .signal_intelligence_models import SignalIntelligenceBriefV1
from .strategy_lab_context import asset_class_mix_hint, format_prior_results

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_IDEATION_SYSTEM = (
    "You are an expert quantitative trading strategy designer for multi-asset swing and short-horizon systems. "
    "You combine several signal families (price/volatility, macro, sentiment, corporate events) into coherent rules. "
    "You explicitly reason about information not in raw OHLCV: news and social sentiment, issuer filings, "
    "macro and micro structure, liquidity regimes, and confounding drivers that backtests may omit. "
    "You diversify across asset classes (stocks, crypto, forex, options, futures, commodities) rather than "
    "defaulting to equities."
)

_IDEATION_PROMPT = """\
Generate ONE novel swing-style strategy (typical holds ~2–14 days unless the asset class implies shorter).
Goal: exceed 8% annualized in principle, with explicit risk controls.

## Prior Strategy Results ({n_prior} tested so far, chronological)
{prior_results_text}

## Asset-class diversity (mandatory)
{asset_class_mix_hint}

{signal_section}

## Multi-signal & confounding factors (mandatory)
Design strategies as a **mixture of signal types**, not a single indicator. At minimum, combine ideas from:
- **Market microstructure / price**: momentum, mean reversion, volatility/volume regimes, cross-asset leads.
- **Macro & micro dynamics**: rates, FX, sector rotation, carry, liquidity, seasonal or event windows.
- **Information not in price alone** (describe how you would operationalize as rules, even if data is proxy/synthetic in backtest): news sentiment, social/media buzz, earnings/guidance or other **issuer financial disclosures**, regulatory/legal catalysts, commodity supply/demand narratives.
Name which confounders you are leaning on and how they interact with price signals.

## Instructions
Each prior entry includes outcome, metrics, rationale, and post-backtest analysis. Generate a strategy that **differs** from prior ones and learns from their failures.
Return ONLY a JSON object with no markdown:
{{
  "asset_class": "stocks" | "crypto" | "forex" | "options" | "futures" | "commodities",
  "hypothesis": "1-3 sentence investment thesis tying multiple signals to edge",
  "signal_definition": "Describe the **ensemble** of signals (e.g. price filter + macro gate + sentiment/filings trigger) and how they combine (AND/OR, scoring, veto rules)",
  "signal_sources": ["list of families used, e.g. price_action, macro_rates, news_sentiment, filings, social_sentiment, cross_asset"],
  "entry_rules": ["rule 1", "rule 2", "rule 3"],
  "exit_rules": ["exit rule 1", "exit rule 2"],
  "sizing_rules": ["sizing rule 1"],
  "risk_limits": {{"max_position_pct": 5, "stop_loss_pct": 3}},
  "speculative": false,
  "rationale": "Why this strategy and asset_class now, given priors and the diversity hint; acknowledge confounders the backtest may only approximate"
}}
"""

_ANALYSIS_DRAFT_SYSTEM = (
    "You are a senior quantitative trading analyst. You reason carefully from evidence: "
    "strategy rules, aggregate backtest metrics, and per-trade simulated results. "
    "You do not invent statistics; every claim must be grounded in the data provided."
)

_ANALYSIS_DRAFT_WIN = """\
Draft a rigorous analysis of this WINNING swing-trading strategy (annualized return above 8% threshold).

## Strategy (definition under test)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing / risk: {sizing_rules}
Rationale for testing: {rationale}

## Aggregated backtest metrics
Annualized return: {annualized_return_pct:.1f}%
Total return: {total_return_pct:.1f}%
Sharpe ratio: {sharpe_ratio:.2f}
Max drawdown: {max_drawdown_pct:.1f}%
Win rate: {win_rate_pct:.1f}%
Profit factor: {profit_factor:.2f}
Volatility: {volatility_pct:.1f}%

## Simulated trade ledger (evidence)
{simulated_trades_section}

## Instructions
Think step by step: what in the strategy design plausibly produced strong risk-adjusted returns?
Relate the hypothesis and rules to (1) Sharpe/drawdown/volatility, (2) win rate vs profit factor, (3) patterns in the simulated trades (hold periods, win/loss mix, concentration).
Write 5–8 sentences. Be specific—avoid generic praise. Explain *why* this strategy class succeeded in this backtest.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
"""

_ANALYSIS_DRAFT_LOSE = """\
Draft a rigorous analysis of this LOSING swing-trading strategy (annualized return below 8% threshold).

## Strategy (definition under test)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing / risk: {sizing_rules}
Rationale for testing: {rationale}

## Aggregated backtest metrics
Annualized return: {annualized_return_pct:.1f}%
Total return: {total_return_pct:.1f}%
Sharpe ratio: {sharpe_ratio:.2f}
Max drawdown: {max_drawdown_pct:.1f}%
Win rate: {win_rate_pct:.1f}%
Profit factor: {profit_factor:.2f}
Volatility: {volatility_pct:.1f}%

## Simulated trade ledger (evidence)
{simulated_trades_section}

## Instructions
Think step by step: what failure modes explain weak performance—signal timing, risk/reward asymmetry, cost drag, or rules misaligned with the market regime implied by the results?
Use the trade-level evidence where it supports your reasoning.
Write 5–8 sentences. Be specific about *why* this strategy underperformed.

Return ONLY JSON with no markdown:
{{"draft_narrative": "your draft analysis"}}
"""

_SELF_REVIEW_SYSTEM = (
    "You are a critical peer reviewer for quantitative research. "
    "You ensure narrative analysis is faithful to strategy specs, backtest aggregates, and simulated trade facts. "
    "You correct any contradiction or overclaim before signing off."
)

_SELF_REVIEW_PROMPT = """\
Perform a self-review of the draft analysis below.

## Strategy facts (source of truth)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}

## Aggregated metrics (source of truth)
Annualized: {annualized_return_pct:.1f}% | Total: {total_return_pct:.1f}% | Sharpe: {sharpe_ratio:.2f}
Max DD: {max_drawdown_pct:.1f}% | Win rate: {win_rate_pct:.1f}% | Profit factor: {profit_factor:.2f} | Vol: {volatility_pct:.1f}%
Outcome label: {outcome_label}

## Simulated trades summary (source of truth)
{simulated_trades_section}

## Draft analysis to verify
{draft_narrative}

## Instructions
1. Check every substantive claim in the draft against the strategy, metrics, and trade evidence.
2. Remove or rewrite anything that is unsupported, vague, or contradicts the numbers.
3. Produce a single polished narrative (5–10 sentences) that a risk committee could rely on.
4. In verification_notes (2–4 sentences), state what you verified and any material corrections.

Return ONLY JSON with no markdown:
{{"revised_narrative": "...", "verification_notes": "..."}}
"""


def _format_simulated_trades_summary(
    trades: List[TradeRecord], *, max_sample_rows: int = 14
) -> str:
    """Compact evidence string from the simulated ledger for analysis + self-review."""
    if not trades:
        return "No simulated trades in ledger."
    n = len(trades)
    wins = sum(1 for t in trades if t.outcome == "win")
    losses = n - wins
    holds = [t.hold_days for t in trades]
    rets = [t.return_pct for t in trades]
    avg_hold = sum(holds) / n
    best_i = max(range(n), key=lambda i: rets[i])
    worst_i = min(range(n), key=lambda i: rets[i])
    tw = trades[best_i]
    tl = trades[worst_i]
    final_cum = trades[-1].cumulative_pnl

    lines = [
        f"Aggregate: {n} simulated trades | {wins} wins / {losses} losses "
        f"({100.0 * wins / n:.1f}% win rate on trades)",
        f"Hold days: avg {avg_hold:.1f}, min {min(holds)}, max {max(holds)}",
        f"Per-trade return %: best {rets[best_i]:.2f}% (trade #{tw.trade_num} {tw.symbol}), "
        f"worst {rets[worst_i]:.2f}% (trade #{tl.trade_num} {tl.symbol})",
        f"Sum of net P&L implied by ledger path; ending cumulative P&L ≈ {final_cum:.2f}",
        "",
        "Sample trades (chronological mix):",
    ]
    # head + tail samples
    indices: List[int] = []
    if n <= max_sample_rows:
        indices = list(range(n))
    else:
        head = max_sample_rows // 2
        tail = max_sample_rows - head
        indices = list(range(head)) + list(range(n - tail, n))

    seen = set()
    for i in indices:
        if i in seen:
            continue
        seen.add(i)
        t = trades[i]
        lines.append(
            f"  #{t.trade_num} {t.symbol} {t.entry_date}→{t.exit_date} "
            f"hold={t.hold_days}d ret={t.return_pct:.2f}% net={t.net_pnl:.2f} "
            f"cum={t.cumulative_pnl:.2f} [{t.outcome}]"
        )
    if n > len(seen):
        lines.append(f"  ... ({n - len(seen)} additional trades not shown) ...")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def _agent_complete_json(agent: Agent, prompt: str) -> Dict[str, Any]:
    """Call an Agent, extract text, and parse as JSON."""
    result = agent(prompt)
    raw = str(result).strip()
    return json.loads(raw)


class StrategyIdeationAgent:
    """
    Uses an LLM to:
      1. Ideate a novel swing trading strategy informed by past results.
      2. Generate a post-backtest narrative explaining success or failure.
    """

    def __init__(self, llm_client=None) -> None:
        _model = get_strands_model("strategy_ideation")
        self._ideation_agent = (
            llm_client
            if llm_client is not None
            else Agent(
                model=_model,
                system_prompt=_IDEATION_SYSTEM,
            )
        )
        self._analysis_draft_agent = (
            llm_client
            if llm_client is not None
            else Agent(
                model=_model,
                system_prompt=_ANALYSIS_DRAFT_SYSTEM,
            )
        )
        self._self_review_agent = (
            llm_client
            if llm_client is not None
            else Agent(
                model=_model,
                system_prompt=_SELF_REVIEW_SYSTEM,
            )
        )

    def ideate_strategy(
        self,
        prior_results: Optional[List[StrategyLabRecord]] = None,
        *,
        precomputed_signal_brief: Optional[SignalIntelligenceBriefV1] = None,
        exclude_asset_classes: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Generate a new strategy spec dict and rationale.

        When ``precomputed_signal_brief`` is set (Policy B: one brief per batch), it is injected
        into the ideation prompt inside guarded delimiters.

        Args:
            exclude_asset_classes: Asset classes to avoid (e.g. because historical data was
                unavailable). Appended as a hard constraint to the mix hint.

        Returns:
            (strategy_dict, rationale) — strategy_dict matches StrategySpec fields,
            rationale is the LLM's explanation for the choice.
        """
        prior_results = prior_results or []
        prior_text = format_prior_results(prior_results)
        mix_hint = asset_class_mix_hint(prior_results)

        if exclude_asset_classes:
            excluded = ", ".join(exclude_asset_classes)
            mix_hint += (
                f"\n\n**HARD CONSTRAINT**: Do NOT use these asset classes (historical data is "
                f"currently unavailable): {excluded}. Pick a different asset class."
            )

        if precomputed_signal_brief is not None:
            inner = brief_to_prompt_block(precomputed_signal_brief)
            signal_section = (
                "## Signal intelligence brief (research lab context; not financial advice)\n"
                "<signal_intelligence_brief>\n"
                f"{inner}\n"
                "</signal_intelligence_brief>"
            )
        else:
            signal_section = ""

        prompt = _IDEATION_PROMPT.format(
            n_prior=len(prior_results),
            prior_results_text=prior_text,
            asset_class_mix_hint=mix_hint,
            signal_section=signal_section,
        )

        data = _agent_complete_json(self._ideation_agent, prompt)

        rationale = str(data.pop("rationale", "No rationale provided."))
        data.pop("signal_sources", None)
        return data, rationale

    def analyze_result(
        self,
        record: StrategyLabRecord,
        rationale: str,
    ) -> str:
        """
        Draft a deep analysis grounded in strategy, metrics, and simulated trades,
        then self-review for consistency and accuracy.
        """
        result = record.backtest.result
        strategy = record.strategy
        trades = record.backtest.trades
        outcome_label = (
            "WINNING (>8% annualized)" if record.is_winning else "LOSING (<8% annualized)"
        )

        simulated_trades_section = _format_simulated_trades_summary(trades)
        sizing_rules = "; ".join(strategy.sizing_rules) if strategy.sizing_rules else "(none)"
        risk_limits = strategy.risk_limits.model_dump_json()

        common = dict(
            asset_class=strategy.asset_class,
            hypothesis=strategy.hypothesis,
            signal_definition=strategy.signal_definition,
            entry_rules="; ".join(strategy.entry_rules),
            exit_rules="; ".join(strategy.exit_rules),
            sizing_rules=f"{sizing_rules} | risk_limits: {risk_limits}",
            rationale=rationale,
            annualized_return_pct=result.annualized_return_pct,
            total_return_pct=result.total_return_pct,
            sharpe_ratio=result.sharpe_ratio,
            max_drawdown_pct=result.max_drawdown_pct,
            win_rate_pct=result.win_rate_pct,
            profit_factor=result.profit_factor,
            volatility_pct=result.volatility_pct,
            simulated_trades_section=simulated_trades_section,
        )

        if record.is_winning:
            draft_prompt = _ANALYSIS_DRAFT_WIN.format(**common)
        else:
            draft_prompt = _ANALYSIS_DRAFT_LOSE.format(**common)

        try:
            draft_data = _agent_complete_json(self._analysis_draft_agent, draft_prompt)
            draft_narrative = str(draft_data.get("draft_narrative", "")).strip()
        except Exception as exc:
            logger.warning("Draft analysis failed: %s", exc)
            draft_narrative = ""

        if not draft_narrative:
            draft_narrative = (
                f"Outcome {outcome_label}: annualized {result.annualized_return_pct:.1f}%, "
                f"Sharpe {result.sharpe_ratio:.2f}, max drawdown {result.max_drawdown_pct:.1f}%."
            )

        review_prompt = _SELF_REVIEW_PROMPT.format(
            asset_class=strategy.asset_class,
            hypothesis=strategy.hypothesis,
            signal_definition=strategy.signal_definition,
            entry_rules="; ".join(strategy.entry_rules),
            exit_rules="; ".join(strategy.exit_rules),
            annualized_return_pct=result.annualized_return_pct,
            total_return_pct=result.total_return_pct,
            sharpe_ratio=result.sharpe_ratio,
            max_drawdown_pct=result.max_drawdown_pct,
            win_rate_pct=result.win_rate_pct,
            profit_factor=result.profit_factor,
            volatility_pct=result.volatility_pct,
            outcome_label=outcome_label,
            simulated_trades_section=simulated_trades_section,
            draft_narrative=draft_narrative,
        )

        try:
            reviewed = _agent_complete_json(self._self_review_agent, review_prompt)
            revised = str(reviewed.get("revised_narrative", "")).strip()
            verification = str(reviewed.get("verification_notes", "")).strip()
        except Exception as exc:
            logger.warning("Self-review failed: %s", exc)
            revised = draft_narrative
            verification = ""

        if not revised:
            revised = draft_narrative

        if verification:
            return f"{revised}\n\n[Self-review: {verification}]"
        return revised
