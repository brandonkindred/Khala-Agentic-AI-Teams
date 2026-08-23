"""Strands Agent for post-backtest narrative analysis (single self-reviewing draft call)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from ...models import WINNING_THRESHOLD, BacktestResult, StrategySpec, TradeRecord
from ._agent_runner import run_single_shot_agent
from ._prompt_context import spec_prompt_fields
from .alignment import TradeAlignmentReport

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Shared stop-order semantics reference (stop-market / stop-limit / trailing
# stop). Concatenated onto the analysis system prompts so the LLM does not
# mislabel correct behavior — chiefly a trailing stop's trigger ratcheting
# above entry as a long appreciates, which is the intended gain-locking
# behavior, not a defect. Read once at import (matches the other prompt loads).
_STOP_ORDER_SEMANTICS = (_PROMPT_DIR / "_stop_order_semantics.md").read_text(encoding="utf-8")

# Shared "How to read the sizing line" block. Single-sourced here (rather than
# duplicated verbatim in analysis_win.md and analysis_lose.md) and injected into
# both draft templates via the {sizing_line_reading} placeholder, so the
# capital-at-risk framing cannot drift between the winning and losing prompts.
# rstrip the trailing newline so the placeholder substitutes cleanly between the
# surrounding blank lines (keeps the rendered prompt byte-identical).
_SIZING_LINE_READING = (
    (_PROMPT_DIR / "_sizing_line_reading.md").read_text(encoding="utf-8").rstrip("\n")
)

# Draft templates + system prompt loaded once at import — static content that was
# previously re-read from disk on every analysis call. The win/lose draft
# templates are keyed by filename so the per-call branch is a dict lookup.
_DRAFT_TEMPLATES = {
    name: (_PROMPT_DIR / name).read_text(encoding="utf-8")
    for name in ("analysis_win.md", "analysis_lose.md")
}
# Combines the drafting persona (analysis_system.md) with the critical-reviewer
# discipline that previously lived in a separate self-review call
# (analysis_review_system.md), so a single call carries both dispositions.
_ANALYSIS_SYSTEM_PROMPT = (
    (_PROMPT_DIR / "analysis_system.md").read_text(encoding="utf-8")
    + "\n\n"
    + (_PROMPT_DIR / "analysis_review_system.md").read_text(encoding="utf-8")
    + "\n\n"
    + _STOP_ORDER_SEMANTICS
)


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

# Trailing section spliced into the draft prompt (see ``_with_self_review_checklist``)
# so the model self-reviews against this checklist before returning its answer,
# instead of a separate follow-up call reviewing the draft's own output.
_SELF_REVIEW_CHECKLIST = """\
## Self-review checklist (apply before finalizing — do not narrate this step)
Before returning your answer, silently verify your draft against every item below and correct any violation in place. Return only the corrected final narrative — never your intermediate reasoning or two versions.
1. Check every substantive claim against the strategy, metrics, and trade evidence above.
{risk_model_check}
1b. Verdict consistency: the WINNING/LOSING label (Outcome label: {outcome_label}) is fixed deterministically — WINNING iff annualized return is at or above the 8% S&P-500 benchmark, LOSING below it. Do NOT reframe a WINNING strategy as a loss (or a LOSING one as a win) on the basis of Sharpe, drawdown, win rate, or the "Robustness caveats" section. Those are honest risk caveats that sit alongside the verdict, never a substitute for it — strike any sentence that contradicts the label.
2. Remove or rewrite anything that is unsupported, vague, or contradicts the numbers.
3. The final narrative must be 5-10 sentences a risk committee could rely on."""

_JSON_INSTRUCTION_MARKER = "Return ONLY JSON with no markdown:"


def _with_self_review_checklist(rendered_prompt: str, checklist: str) -> str:
    """Splice the self-review checklist into a rendered draft prompt.

    Preconditions: ``rendered_prompt`` contains exactly one occurrence of
    :data:`_JSON_INSTRUCTION_MARKER` (guaranteed by ``analysis_win.md`` /
    ``analysis_lose.md``, which both end with that instruction).
    Postconditions: returns ``rendered_prompt`` with ``checklist`` inserted
    immediately before the JSON-return instruction, separated by blank lines,
    so the format directive stays the model's last-read instruction.
    """
    prefix, marker, suffix = rendered_prompt.partition(_JSON_INSTRUCTION_MARKER)
    assert marker, "draft template must contain the JSON-return instruction marker"
    return f"{prefix}{checklist}\n\n{marker}{suffix}"


_MISALIGNED_DISCLAIMER = (
    "The executed trades did not faithfully implement the specification; "
    "interpretation is preliminary."
)


class AnalysisAgent:
    """Generate a self-reviewed post-backtest narrative analysis in one LLM call."""

    def run(
        self,
        spec: StrategySpec,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        rationale: str,
        is_winning: Optional[bool] = None,
        alignment_report: Optional[TradeAlignmentReport] = None,
        robustness_caveats: Optional[str] = None,
    ) -> str:
        """Produce a polished, self-reviewed analysis narrative in a single LLM call.

        Args:
            is_winning: Authoritative verdict from the orchestrator. When None,
                falls back to a simplified return-only check
                (``annualized_return_pct >= WINNING_THRESHOLD``). This is NOT
                identical to the orchestrator's rule: the orchestrator also
                applies the ``execution_succeeded and trades`` validity
                precondition, which the agent cannot evaluate from a
                ``BacktestResult`` alone (an invalid run with no trades but a
                computed return would fall back to WINNING here while the
                orchestrator labels it LOSING). Callers that know the execution
                context (the orchestrator) MUST therefore pass the authoritative
                ``is_winning`` so the narrative template and ``outcome_label``
                stay consistent with the persisted ``StrategyLabRecord.is_winning``.
                The label is purely the 8% S&P-500 benchmark verdict — robustness
                diagnostics never change it; they surface as caveats only (see
                ``robustness_caveats``).
            robustness_caveats: Pre-rendered ``## Robustness caveats`` block to
                inject into the draft prompt. When None, it is derived from
                ``metrics`` via :func:`format_robustness_caveats`
                (acceptance_reason + out-of-sample diagnostics), so the narrative
                can cite walk-forward / DSR / regime concerns as honest caveats on
                a winner without reframing it as a loss. Empty string when there
                are no concerns, keeping clean-run prompts byte-identical.
            alignment_report: Latest ``TradeAlignmentReport`` from the alignment
                loop. When ``aligned=False``, the draft prompt surfaces a
                disclaimer + the concrete alignment issues and forbids causal
                claims about strategy design (#532). When None or
                ``aligned=True``, the section is empty / a one-line affirmation
                so legacy callers and clean runs are unaffected.

        Returns the final narrative string.
        """
        if is_winning is None:
            is_winning = metrics.annualized_return_pct >= WINNING_THRESHOLD
        trades_summary = _format_simulated_trades_summary(trades)
        alignment_section = _format_alignment_status_section(alignment_report)
        caveats_section = (
            robustness_caveats
            if robustness_caveats is not None
            else format_robustness_caveats(metrics)
        )

        # Single self-reviewing draft call: the self-review checklist is spliced
        # into the draft prompt as a trailing section (see
        # ``_with_self_review_checklist``) so the model verifies its own claims
        # while drafting rather than in a separate follow-up call.
        template_file = "analysis_win.md" if is_winning else "analysis_lose.md"
        draft_template = _DRAFT_TEMPLATES[template_file]
        system_prompt = _ANALYSIS_SYSTEM_PROMPT

        draft_prompt = draft_template.format(
            **spec_prompt_fields(spec),
            sizing_line_reading=_SIZING_LINE_READING,
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
            robustness_caveats_section=caveats_section,
        )
        checklist = _SELF_REVIEW_CHECKLIST.format(
            risk_model_check=_RISK_MODEL_CHECK,
            outcome_label="WINNING" if is_winning else "LOSING",
        )
        draft_prompt = _with_self_review_checklist(draft_prompt, checklist)

        def _on_draft_failure(exc: Exception) -> str:
            logger.exception("Draft analysis failed")
            return _fallback_narrative(spec, metrics, is_winning, alignment_report)

        # Keep ``guard_design_budget=True`` even under ``charge=False``: a budget
        # trip is a cycle-level stop and must propagate bare rather than being
        # handed to ``on_failure`` (which would swallow it into
        # ``_fallback_narrative``). That stays correct if charging is ever
        # enabled at this site.
        ok, draft_parsed = run_single_shot_agent(
            agent_key="strategy_analysis",
            phase="analysis_draft",
            system_prompt=system_prompt,
            user_prompt=draft_prompt,
            charge=False,
            guard_design_budget=True,
            logger=logger,
            on_failure=_on_draft_failure,
        )
        if not ok:
            return draft_parsed
        draft_narrative = draft_parsed.get("draft_narrative", "")

        if not draft_narrative:
            return _fallback_narrative(spec, metrics, is_winning, alignment_report)

        return _ensure_misalignment_disclaimer(draft_narrative, alignment_report)


def _sanitize_exit_reason(reason: str, max_len: int = 80) -> str:
    """Render a free-form exit reason as a bounded single-line value.

    ``exit_reason`` derives from ``OrderRequest.reason``, a strategy-controlled
    (untrusted, possibly LLM- or user-generated) annotation. Writing it raw into
    the ledger row would let a multi-line or oversized reason — e.g.
    ``"engine_exit:stop_loss\\nIgnore previous instructions..."`` — break out of
    its row and inject prompt text. Collapse all whitespace to single spaces so
    it cannot span lines, and bound the length so it cannot flood the prompt.

    Preconditions: ``reason`` is a string (may be empty or whitespace-only).
    Postconditions: the result contains no newline/tab, is ``<= max_len``
    characters, and is empty only if ``reason`` was empty or whitespace-only.
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


# ``acceptance_reason`` values that carry no robustness caveat to surface.
# Two kinds qualify:
#   1. A clean robustness pass ("all four criteria met", a clean walk-forward
#      fallback via ``_CLEAN_ACCEPTANCE_PREFIXES``, or no gates evaluated).
#   2. The ``publication_disabled:`` *validity-precondition* reasons that mean
#      no genuine run happened at all (execution failed, or it produced no
#      trades). These are precondition failures, not out-of-sample/robustness
#      diagnostics, so the "## Robustness caveats" block — whose header promises
#      OOS / robustness findings — must not render them or it mislabels the cause.
# Every *other* non-empty ``acceptance_reason`` was written by the verification
# phase to record a real concern (a failing acceptance sub-criterion, a fallback
# rejection, a conformance / realism / alignment / look-ahead veto, or
# ``publication_disabled: walk_forward_enabled=False`` — a genuine "ran but was
# not out-of-sample validated" caveat), so it still becomes a caveat.
_CLEAN_ACCEPTANCE_REASONS = frozenset(
    {
        "all four criteria met",
        "no acceptance gates evaluated",
        "publication_disabled: no trades produced",
        "publication_disabled: execution_failed",
    }
)
_CLEAN_ACCEPTANCE_PREFIXES = ("walk_forward_fallback_passed",)


def format_robustness_caveats(metrics: BacktestResult) -> str:
    """Render the ``## Robustness caveats`` block injected into the analysis prompts.

    The Strategy Lab's WINNING/LOSING label is fixed deterministically by
    annualized return vs the 8% S&P-500 benchmark; the walk-forward acceptance
    gate, alignment, conformance, and realism gates no longer decide it. This
    block carries their recorded findings — ``metrics.acceptance_reason`` (the
    curated cause string the verification phase stamps) plus the out-of-sample
    diagnostics — so the narrative can cite them as honest risk caveats on a
    winner rather than as grounds to reframe it as a loss.

    Returns ``""`` when there are no robustness concerns to surface (a clean
    acceptance pass, a clean fallback, a run with no recorded reason, or a
    ``publication_disabled:`` validity-precondition reason such as execution
    failure / no trades — those are not robustness diagnostics), so clean-run
    prompts render byte-identical to before. Otherwise returns a
    compact block beginning with ``"## Robustness caveats"`` and ending with a
    single trailing newline (so the caller can splice it directly before the
    next section).

    Preconditions: ``metrics`` is a populated :class:`BacktestResult`.
    Postconditions: result is ``""`` or a ``str`` starting with
    ``"## Robustness caveats"`` and ending with ``"\\n"``; the function is pure
    (no mutation, no I/O).
    """
    reason = (metrics.acceptance_reason or "").strip()
    reason_is_concern = bool(reason) and (
        reason not in _CLEAN_ACCEPTANCE_REASONS
        and not any(reason.startswith(prefix) for prefix in _CLEAN_ACCEPTANCE_PREFIXES)
    )
    if not reason_is_concern:
        return ""

    lines: List[str] = [
        "## Robustness caveats (source of truth — NOT grounds to change the verdict)",
        "Out-of-sample / robustness diagnostics recorded by the verification gates. The "
        "WINNING/LOSING label is fixed by annualized return vs the 8% S&P-500 benchmark; cite "
        "these only as honest risk caveats, never to reframe a winning strategy as a loss.",
        f"- Recorded verdict-gate finding: {reason}",
    ]
    diag: List[str] = []
    if metrics.oos_sharpe is not None:
        diag.append(f"OOS Sharpe {metrics.oos_sharpe:.2f}")
        if metrics.deflated_sharpe is not None:
            diag.append(f"deflated Sharpe {metrics.deflated_sharpe:.2f}")
        if metrics.is_oos_degradation_pct is not None:
            diag.append(f"IS→OOS Sharpe degradation {metrics.is_oos_degradation_pct:.1f}%")
        if metrics.oos_trade_count is not None:
            diag.append(f"OOS trades {metrics.oos_trade_count}")
        regimes = metrics.regime_results or []
        if regimes:
            beats = sum(1 for regime in regimes if regime.get("beat_benchmark"))
            diag.append(f"beat benchmark in {beats} of {len(regimes)} regime subwindows")
    if diag:
        lines.append("- Out-of-sample diagnostics: " + "; ".join(diag) + ".")

    return "\n".join(lines) + "\n"


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
