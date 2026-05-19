"""Strands Agent for post-backtest narrative analysis (draft + self-review)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from strands import Agent

from ...models import BacktestResult, StrategySpec, TradeRecord
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
from .alignment import TradeAlignmentReport
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

{alignment_status_section}
## Draft analysis to verify
{draft_narrative}

## Instructions
0. If an "Alignment status" section above marks the run as misaligned, ensure the polished narrative opens with the disclaimer verbatim and contains no causal claims about strategy design ("worked because of X", "failed because of Y"). Treat the listed alignment issues as facts; do not soften them.
1. Check every substantive claim in the draft against the strategy, metrics, and trade evidence.
2. Remove or rewrite anything that is unsupported, vague, or contradicts the numbers.
3. Produce a single polished narrative (5-10 sentences) that a risk committee could rely on.
4. In verification_notes (2-4 sentences), state what you verified and any material corrections.

Return ONLY JSON with no markdown:
{{"revised_narrative": "...", "verification_notes": "..."}}
"""

_MISALIGNED_DISCLAIMER = (
    "The executed trades did not faithfully implement the specification; "
    "interpretation is preliminary."
)


class AnalysisAgent:
    """Generate and self-review a post-backtest narrative analysis."""

    def run(
        self,
        spec: StrategySpec,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        rationale: str,
        on_sub_phase: Any = None,
        is_winning: Optional[bool] = None,
        alignment_report: Optional[TradeAlignmentReport] = None,
    ) -> str:
        """Produce a polished analysis narrative via draft + self-review.

        Args:
            on_sub_phase: Optional callback ``(sub_phase: str) -> None`` for progress.
            is_winning: Authoritative verdict from the orchestrator. When None,
                falls back to the legacy metric-only heuristic; callers that
                resolve ``is_winning`` against the alignment loop / acceptance
                gate / fallback anomalies (#529) must pass it explicitly so the
                narrative template and ``outcome_label`` match the persisted
                ``StrategyLabRecord.is_winning``.
            alignment_report: Latest ``TradeAlignmentReport`` from the alignment
                loop. When ``aligned=False``, both the draft and self-review
                prompts surface a disclaimer + the concrete alignment issues and
                forbid causal claims about strategy design (#532). When None or
                ``aligned=True``, the section is empty / a one-line affirmation
                so legacy callers and clean runs are unaffected.

        Returns the final narrative string.
        """
        if is_winning is None:
            is_winning = metrics.annualized_return_pct > 8.0
        trades_summary = _format_simulated_trades_summary(trades)
        alignment_section = _format_alignment_status_section(alignment_report)

        # Phase 1: Draft
        template_file = "analysis_win.md" if is_winning else "analysis_lose.md"
        draft_template = (_PROMPT_DIR / template_file).read_text(encoding="utf-8")
        system_prompt = (_PROMPT_DIR / "analysis_system.md").read_text(encoding="utf-8")

        draft_prompt = draft_template.format(
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            signal_definition=spec.signal_definition,
            entry_rules=format_rules_for_prompt(spec.entry_rules),
            exit_rules=format_rules_for_prompt(spec.exit_rules),
            sizing_rules=format_sizing_rule(spec.sizing),
            rationale=rationale,
            annualized_return_pct=metrics.annualized_return_pct,
            total_return_pct=metrics.total_return_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            win_rate_pct=metrics.win_rate_pct,
            profit_factor=metrics.profit_factor,
            volatility_pct=metrics.volatility_pct,
            simulated_trades_section=trades_summary,
            alignment_status_section=alignment_section,
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
            return _fallback_narrative(spec, metrics, is_winning, alignment_report)

        if not draft_narrative:
            return _fallback_narrative(spec, metrics, is_winning, alignment_report)

        # Phase 2: Self-review
        if on_sub_phase:
            on_sub_phase("review")
        review_prompt = _SELF_REVIEW_PROMPT.format(
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            signal_definition=spec.signal_definition,
            entry_rules=format_rules_for_prompt(spec.entry_rules),
            exit_rules=format_rules_for_prompt(spec.exit_rules),
            annualized_return_pct=metrics.annualized_return_pct,
            total_return_pct=metrics.total_return_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            win_rate_pct=metrics.win_rate_pct,
            profit_factor=metrics.profit_factor,
            volatility_pct=metrics.volatility_pct,
            outcome_label="WINNING" if is_winning else "LOSING",
            simulated_trades_section=trades_summary,
            alignment_status_section=alignment_section,
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
                return _ensure_misalignment_disclaimer(revised, alignment_report)
        except Exception:
            logger.exception("Self-review failed, using draft")

        return _ensure_misalignment_disclaimer(draft_narrative, alignment_report)


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


def _format_alignment_status_section(report: Optional[TradeAlignmentReport]) -> str:
    """Render the ``## Alignment status`` block injected into analysis prompts.

    ``None`` produces an empty string so legacy callers (and fallback paths
    where no alignment report exists) render byte-identical prompts to before
    issue #532. ``aligned=True`` produces a one-line affirmation. ``aligned=
    False`` produces a disclaimer, the enumerated audit issues, and explicit
    instructions that forbid causal claims about strategy design — the trades
    are not a valid test of the spec, so the narrative must not say it
    "worked because of X" or "failed because of Y".
    """
    if report is None:
        return ""

    if report.aligned:
        return (
            "## Alignment status\n"
            "The executed trades faithfully implement the specification "
            "(alignment audit clean).\n"
        )

    lines: List[str] = [
        "## Alignment status — TRADES DID NOT IMPLEMENT THE SPEC",
        "",
        f'Disclaimer to surface in the narrative: "{_MISALIGNED_DISCLAIMER}"',
        "",
        "Concrete alignment issues (facts; do not paraphrase away):",
    ]
    if report.issues:
        for issue in report.issues:
            lines.append(f"- [{issue.severity}] {issue.rule_type}: {issue.description}")
    else:
        lines.append("- (audit returned aligned=False with no enumerated issues)")
    if report.rationale:
        lines.append("")
        lines.append(f"Audit rationale: {report.rationale}")
    lines.extend(
        [
            "",
            "Constraints for this analysis:",
            "- DO open the narrative with the disclaimer above.",
            '- DO NOT make causal claims about strategy design (e.g. "worked because of X", "failed because of Y") — the trades are not a valid test of the spec.',
            "- DO describe execution gaps factually and recommend re-running once aligned.",
            "",
        ]
    )
    return "\n".join(lines)


def format_misalignment_prefix(report: Optional[TradeAlignmentReport]) -> str:
    """Disclaimer + enumerated issues block for narrative outputs when
    ``aligned=False``.

    Returns an empty string for ``None`` or ``aligned=True`` so aligned and
    legacy paths stay byte-identical. Shared between the agent's
    ``_fallback_narrative`` and the orchestrator's analysis-phase exception
    handler so a misaligned run cannot slip a confident auto-summary out
    via either fallback path (#532).
    """
    if report is None or report.aligned:
        return ""
    parts: List[str] = [_MISALIGNED_DISCLAIMER]
    if report.issues:
        parts.append("Alignment issues:")
        for issue in report.issues:
            parts.append(f"- [{issue.severity}] {issue.rule_type}: {issue.description}")
    return "\n".join(parts)


def _ensure_misalignment_disclaimer(
    narrative: str, alignment_report: Optional[TradeAlignmentReport]
) -> str:
    """Deterministically guarantee the misalignment disclaimer is present.

    The LLM is *told* to open misaligned narratives with the disclaimer
    verbatim, but cannot be trusted to comply — and the self-review pass
    that's meant to enforce it may fail or return the raw draft. On
    ``aligned=False`` runs this prepends the full ``format_misalignment_prefix``
    block when the disclaimer string is missing, so the safety rail holds
    even when the LLM ignores the instruction (#532, Codex follow-up on
    PR #584).

    No-ops on aligned / None reports and on narratives that already
    contain the disclaimer string verbatim, so compliant LLM output and
    legacy callers stay byte-identical.
    """
    prefix = format_misalignment_prefix(alignment_report)
    if not prefix:
        return narrative
    if _MISALIGNED_DISCLAIMER in narrative:
        return narrative
    return f"{prefix}\n\n{narrative}"


def _fallback_narrative(
    spec: StrategySpec,
    metrics: BacktestResult,
    is_winning: bool,
    alignment_report: Optional[TradeAlignmentReport] = None,
) -> str:
    """Auto-generated fallback when LLM analysis fails.

    When the draft LLM call raises, returns unparseable JSON, or yields an
    empty narrative, this deterministic summary takes over. If
    ``alignment_report.aligned`` is False the summary is prefixed with the
    misalignment disclaimer and the audit's enumerated issues, so a
    fail-closed audit error or transient LLM outage cannot publish a
    confident auto-summary on a run that didn't faithfully implement the
    spec (#532).
    """
    label = "winning" if is_winning else "losing"
    summary = (
        f"Auto-summary: {spec.asset_class} strategy ({label}) with annualized return "
        f"{metrics.annualized_return_pct:.1f}%, Sharpe {metrics.sharpe_ratio:.2f}, "
        f"max drawdown {metrics.max_drawdown_pct:.1f}%, win rate {metrics.win_rate_pct:.1f}%. "
        f"(Detailed narrative generation failed.)"
    )
    prefix = format_misalignment_prefix(alignment_report)
    if not prefix:
        return summary
    return f"{prefix}\n{summary}"


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
