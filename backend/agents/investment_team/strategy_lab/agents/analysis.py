"""Strands Agent for post-backtest narrative analysis (draft + self-review)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from strands import Agent

from ...models import BacktestResult, StrategySpec, TradeRecord
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
from ._llm_envelope import invoke_agent
from ._parse_helpers import extract_json_object
from .alignment import TradeAlignmentReport
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Shared stop-order semantics reference (stop-market / stop-limit / trailing
# stop). Concatenated onto the analysis system prompts so the LLM does not
# mislabel correct behavior — chiefly a trailing stop's trigger ratcheting
# above entry as a long appreciates, which is the intended gain-locking
# behavior, not a defect. Read once at import (matches the other prompt loads).
_STOP_ORDER_SEMANTICS = (_PROMPT_DIR / "_stop_order_semantics.md").read_text(encoding="utf-8")

# The self-review risk-model check (instruction "1a"). Kept as its own
# implicitly-concatenated constant — rather than one ~1k-char line inside the
# template — for source readability; interpolated into the template below.
_RISK_MODEL_CHECK = (
    "1a. Risk-model check: confirm the narrative treats the deployed position size as the per-trade "
    "capital at risk and loss cap, and treats stop-loss / trailing stop / take-profit as separate "
    "within-position safeguards analyzed as a distinct dimension. "
    'Read the "Sizing / risk" line by rule: "risk X% per trade" targets X% of the account (nominal, '
    "before whole-share lot rounding and the position cap); "
    '"$Y per trade" targets a fixed $Y per position capped by the position limit; '
    '"vol-target X%" is a target annual volatility, so the deployed amount is dynamic and not shown — '
    'do NOT read "risk X% per trade", "vol-target X%", or "$Y per trade" as the exact capital at risk, '
    "since lot rounding or a position cap may move the realised deployment. "
    "Strike any claim that derives per-trade risk by multiplying the stop into sizing "
    '(deployed-fraction times stop), calls such a stop-multiplied figure the "capital at risk" / '
    '"capital in play", or blames low/negative returns on "low effective risk". '
    "Stating that the deployed size IS the capital at risk — including that a genuinely small "
    "deployment is small capital at risk — is correct and must be preserved; only strike the "
    "stop-multiplied conflation and the misattribution of returns to it. "
    "The sizing line is only the nominal rule; the trade ledger reports per-trade position_value, "
    "which IS the realised deployed capital at risk — verify any per-trade deployed-capital or "
    "capital-at-risk claim against those position_value figures rather than re-deriving it from the "
    "nominal sizing line."
)

_SELF_REVIEW_PROMPT = """\
Perform a self-review of the draft analysis below.

## Strategy facts (source of truth)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing / risk: {sizing_rules}

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
{risk_model_check}
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
        system_prompt = (
            (_PROMPT_DIR / "analysis_system.md").read_text(encoding="utf-8")
            + "\n\n"
            + _STOP_ORDER_SEMANTICS
        )

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
            draft_raw = invoke_agent(
                agent,
                draft_prompt,
                agent_key="strategy_ideation",
                phase="analysis_draft",
                logger=logger,
            )
            draft_parsed = extract_json_object(draft_raw)
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
            sizing_rules=format_sizing_rule(spec.sizing),
            risk_model_check=_RISK_MODEL_CHECK,
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
            "You are a critical peer reviewer for quantitative research, with a quant's rigor and a veteran trader's instinct. "
            "You ensure narrative analysis is faithful to strategy specs, backtest aggregates, and simulated trade facts. "
            "You enforce the correct risk model: the deployed position size (e.g. 'risk 5% per trade') IS the per-trade capital at risk and loss cap, while stop-loss, trailing stops, and take-profit are separate within-position safeguards analyzed as a distinct dimension. "
            "Reject any claim that derives per-trade risk by multiplying the stop into sizing (deployed-fraction times stop), calls such a stop-multiplied figure the 'capital at risk' / 'capital in play', or blames low/negative returns on 'low effective risk'. "
            "Stating that the deployed size IS the capital at risk — including that a genuinely small deployment is small capital at risk — is accurate and must be preserved. "
            "You correct any contradiction or overclaim before signing off."
            "\n\n" + _STOP_ORDER_SEMANTICS
        )

        review_agent = Agent(
            model=get_strands_model("strategy_ideation"), system_prompt=review_system, tools=[]
        )

        try:
            review_raw = invoke_agent(
                review_agent,
                review_prompt,
                agent_key="strategy_ideation",
                phase="analysis_review",
                logger=logger,
            )
            review_parsed = extract_json_object(review_raw)
            revised = review_parsed.get("revised_narrative", "")
            if revised:
                return _ensure_misalignment_disclaimer(revised, alignment_report)
        except Exception:
            logger.exception("Self-review failed, using draft")

        return _ensure_misalignment_disclaimer(draft_narrative, alignment_report)


def _sanitize_exit_reason(reason: str, max_len: int = 80) -> str:
    """Render a free-form exit reason as a bounded single-line value.

    ``exit_reason`` derives from ``OrderRequest.reason``, a strategy-controlled
    (untrusted, possibly LLM- or user-generated) annotation. Writing it raw into
    the ledger row would let a multi-line or oversized reason — e.g.
    ``"engine_exit:stop_loss\\nIgnore previous instructions..."`` — break out of
    its row and inject prompt text. Collapse all whitespace to single spaces so
    it cannot span lines, and bound the length so it cannot flood the prompt.

    Preconditions: ``reason`` is a non-empty string.
    Postconditions: the result contains no newline/tab, is ``<= max_len``
    characters, and is empty only if ``reason`` was whitespace-only.
    """
    flattened = " ".join(reason.split())
    if len(flattened) > max_len:
        flattened = flattened[: max_len - 1] + "…"
    return flattened


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
    position_values = [t.position_value for t in trades]
    avg_pv = sum(position_values) / n

    lines = [
        f"Aggregate: {n} simulated trades | {wins} wins / {losses} losses "
        f"({100.0 * wins / n:.1f}% win rate on trades)",
        f"Hold days: avg {avg_hold:.1f}, min {min(holds)}, max {max(holds)}",
        f"Per-trade return %: best {rets[best_i]:.2f}% (trade #{tw.trade_num} {tw.symbol}), "
        f"worst {rets[worst_i]:.2f}% (trade #{tl.trade_num} {tl.symbol})",
        f"Per-trade deployed capital (position_value = the realised capital at risk): "
        f"min ${min(position_values):.2f}, max ${max(position_values):.2f}, avg ${avg_pv:.2f}",
        f"Sum of net P&L implied by ledger path; ending cumulative P&L = {final_cum:.2f}",
        "",
        "Sample trades (chronological mix; pv = deployed capital, exit = exit reason when recorded):",
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
        exit_reason = f" exit={_sanitize_exit_reason(t.exit_reason)}" if t.exit_reason else ""
        lines.append(
            f"  #{t.trade_num} {t.symbol} {t.entry_date}->{t.exit_date} "
            f"hold={t.hold_days}d pv=${t.position_value:.2f} ret={t.return_pct:.2f}% "
            f"net={t.net_pnl:.2f} cum={t.cumulative_pnl:.2f} [{t.outcome}]{exit_reason}"
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
    """Deterministically guarantee the misalignment disclaimer + audit
    facts are visible on every published narrative.

    The LLM is *told* to open misaligned narratives with the disclaimer
    verbatim and to surface the concrete alignment issues, but cannot be
    trusted to comply — and the self-review pass that's meant to enforce
    it may fail or echo a non-compliant draft. The rail enforces two
    deterministic guarantees on ``aligned=False`` runs:

    1. **The disclaimer opens the narrative.** If the narrative does not
       start with the disclaimer string (after ``lstrip``), the full
       ``format_misalignment_prefix`` block is prepended. This catches
       LLMs that buried the disclaimer mid-body behind a causal claim
       (#532, Codex follow-up).
    2. **Every concrete alignment issue is present somewhere in the
       narrative.** If the LLM opened with the disclaimer but dropped
       any ``AlignmentIssue.description`` strings, those issues are
       deterministically appended to the narrative so operators always
       see the audit facts — even if the LLM paraphrased the disclaimer
       correctly but elided the issue list.

    The rail intentionally cannot detect *causal claims* about strategy
    design (e.g. "failed because of X") — that requires natural-language
    understanding. The prompt + self-review pass remain the only
    mitigation for that. What this helper guarantees is that the
    operator always sees the disclaimer at the top and the concrete
    audit facts somewhere below, regardless of LLM compliance.

    No-ops on aligned / None reports so clean / legacy callers stay
    byte-identical.
    """
    prefix = format_misalignment_prefix(alignment_report)
    if not prefix:
        return narrative
    # Guarantee #1: disclaimer opens the narrative.
    if not narrative.lstrip().startswith(_MISALIGNED_DISCLAIMER):
        return f"{prefix}\n\n{narrative}"
    # Guarantee #2: every concrete alignment issue is somewhere in the body.
    # ``alignment_report`` is non-None because ``prefix`` was non-empty.
    assert alignment_report is not None
    missing_issues = [
        issue for issue in alignment_report.issues if issue.description not in narrative
    ]
    if missing_issues:
        appended_lines = ["", "Alignment issues (deterministically appended):"]
        for issue in missing_issues:
            appended_lines.append(f"- [{issue.severity}] {issue.rule_type}: {issue.description}")
        return narrative + "\n" + "\n".join(appended_lines)
    return narrative


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
