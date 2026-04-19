"""Strands Agent for post-backtest narrative analysis (draft + self-review)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from strands import Agent

from ...models import BacktestResult, StrategySpec, TradeRecord
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

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
3. Produce a single polished narrative (5-10 sentences) that a risk committee could rely on.
4. In verification_notes (2-4 sentences), state what you verified and any material corrections.

Return ONLY JSON with no markdown:
{{"revised_narrative": "...", "verification_notes": "..."}}
"""


class AnalysisAgent:
    """Generate and self-review a post-backtest narrative analysis."""

    def run(
        self,
        spec: StrategySpec,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        rationale: str,
        on_sub_phase: Any = None,
    ) -> str:
        """Produce a polished analysis narrative via draft + self-review.

        Args:
            on_sub_phase: Optional callback ``(sub_phase: str) -> None`` for progress.

        Returns the final narrative string.
        """
        is_winning = metrics.annualized_return_pct > 8.0
        trades_summary = _format_simulated_trades_summary(trades)

        # Phase 1: Draft
        template_file = "analysis_win.md" if is_winning else "analysis_lose.md"
        draft_template = (_PROMPT_DIR / template_file).read_text(encoding="utf-8")
        system_prompt = (_PROMPT_DIR / "analysis_system.md").read_text(encoding="utf-8")

        draft_prompt = draft_template.format(
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            signal_definition=spec.signal_definition,
            entry_rules=", ".join(spec.entry_rules),
            exit_rules=", ".join(spec.exit_rules),
            sizing_rules=", ".join(spec.sizing_rules),
            rationale=rationale,
            annualized_return_pct=metrics.annualized_return_pct,
            total_return_pct=metrics.total_return_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            win_rate_pct=metrics.win_rate_pct,
            profit_factor=metrics.profit_factor,
            volatility_pct=metrics.volatility_pct,
            simulated_trades_section=trades_summary,
        )

        agent = Agent(
            model=get_strands_model("strategy_ideation"), system_prompt=system_prompt, tools=[]
        )

        try:
            draft_result = agent(draft_prompt)
            draft_parsed = _extract_json(str(draft_result))
            draft_narrative = draft_parsed.get("draft_narrative", "")
        except Exception:
            logger.exception("Draft analysis failed")
            return _fallback_narrative(spec, metrics, is_winning)

        if not draft_narrative:
            return _fallback_narrative(spec, metrics, is_winning)

        # Phase 2: Self-review
        if on_sub_phase:
            on_sub_phase("review")
        review_prompt = _SELF_REVIEW_PROMPT.format(
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            signal_definition=spec.signal_definition,
            entry_rules=", ".join(spec.entry_rules),
            exit_rules=", ".join(spec.exit_rules),
            annualized_return_pct=metrics.annualized_return_pct,
            total_return_pct=metrics.total_return_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            win_rate_pct=metrics.win_rate_pct,
            profit_factor=metrics.profit_factor,
            volatility_pct=metrics.volatility_pct,
            outcome_label="WINNING" if is_winning else "LOSING",
            simulated_trades_section=trades_summary,
            draft_narrative=draft_narrative,
        )

        review_system = (
            "You are a critical peer reviewer for quantitative research. "
            "You ensure narrative analysis is faithful to strategy specs, backtest aggregates, and simulated trade facts. "
            "You correct any contradiction or overclaim before signing off."
        )

        review_agent = Agent(
            model=get_strands_model("strategy_ideation"), system_prompt=review_system, tools=[]
        )

        try:
            review_result = review_agent(review_prompt)
            review_parsed = _extract_json(str(review_result))
            revised = review_parsed.get("revised_narrative", "")
            if revised:
                return revised
        except Exception:
            logger.exception("Self-review failed, using draft")

        return draft_narrative


def _format_simulated_trades_summary(trades: List[TradeRecord], max_sample_rows: int = 14) -> str:
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
        f"Sum of net P&L implied by ledger path; ending cumulative P&L = {final_cum:.2f}",
        "",
        "Sample trades (chronological mix):",
    ]
    indices: List[int] = []
    if n <= max_sample_rows:
        indices = list(range(n))
    else:
        head = max_sample_rows // 2
        tail = max_sample_rows - head
        indices = list(range(head)) + list(range(n - tail, n))

    seen: set[int] = set()
    for i in indices:
        if i in seen:
            continue
        seen.add(i)
        t = trades[i]
        lines.append(
            f"  #{t.trade_num} {t.symbol} {t.entry_date}->{t.exit_date} "
            f"hold={t.hold_days}d ret={t.return_pct:.2f}% net={t.net_pnl:.2f} "
            f"cum={t.cumulative_pnl:.2f} [{t.outcome}]"
        )
    if n > len(seen):
        lines.append(f"  ... ({n - len(seen)} additional trades not shown) ...")

    return "\n".join(lines)


def _fallback_narrative(spec: StrategySpec, metrics: BacktestResult, is_winning: bool) -> str:
    """Auto-generated fallback when LLM analysis fails."""
    label = "winning" if is_winning else "losing"
    return (
        f"Auto-summary: {spec.asset_class} strategy ({label}) with annualized return "
        f"{metrics.annualized_return_pct:.1f}%, Sharpe {metrics.sharpe_ratio:.2f}, "
        f"max drawdown {metrics.max_drawdown_pct:.1f}%, win rate {metrics.win_rate_pct:.1f}%. "
        f"(Detailed narrative generation failed.)"
    )


def _extract_json(text: str) -> Dict[str, Any]:
    """Extract a JSON object from LLM output."""
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in LLM response")
    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(text[start:end])
