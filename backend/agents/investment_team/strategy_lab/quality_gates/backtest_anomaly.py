"""Threshold-based anomaly detector for backtest results."""

from __future__ import annotations

from typing import ClassVar, List, Optional

from ...models import BacktestExecutionDiagnostics, BacktestResult, CoverageReport, TradeRecord
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "backtest_anomaly"

_GENERIC_ZERO_TRADE_DETAILS = (
    "Backtest produced zero trades — strategy code never entered a position."
)


def _format_zero_trade_details(
    diagnostics: Optional[BacktestExecutionDiagnostics],
    coverage_report: Optional[CoverageReport] = None,
) -> str:
    """Build the ``QualityGateResult.details`` string for a zero-trade backtest.

    When ``diagnostics`` carries a deterministic ``zero_trade_category`` (see
    issue #404), surface the category, the executor's summary, the order
    counters, and any rejection-reason histogram so the refinement agent has
    enough evidence to repair the entry/exit path. Falls back to the
    historical generic message when diagnostics are missing or the executor
    couldn't classify the failure.

    When ``coverage_report`` is also present (issue #452), append a one-line
    ``Coverage: <category> — <summary>`` so the persisted gate result records
    the deterministic rule-coverage verdict from the #451 probe alongside the
    executor's view.
    """
    coverage_line = _format_coverage_line(coverage_report)

    if diagnostics is None or diagnostics.zero_trade_category is None:
        if coverage_line:
            return f"{_GENERIC_ZERO_TRADE_DETAILS} {coverage_line}"
        return _GENERIC_ZERO_TRADE_DETAILS

    parts: List[str] = [
        f"Backtest produced zero trades — Category: {diagnostics.zero_trade_category}."
    ]
    if diagnostics.summary:
        parts.append(diagnostics.summary)

    counters = (
        f"orders_emitted={diagnostics.orders_emitted} "
        f"orders_accepted={diagnostics.orders_accepted} "
        f"orders_rejected={diagnostics.orders_rejected} "
        f"orders_unfilled={diagnostics.orders_unfilled} "
        f"warmup_orders_dropped={diagnostics.warmup_orders_dropped} "
        f"entries_filled={diagnostics.entries_filled} "
        f"exits_emitted={diagnostics.exits_emitted}"
    )
    parts.append(counters)

    if diagnostics.orders_rejection_reasons:
        reasons = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(diagnostics.orders_rejection_reasons.items())
        )
        parts.append(f"rejection_reasons: {reasons}")

    if coverage_line:
        parts.append(coverage_line)

    return " ".join(parts)


def _format_coverage_line(coverage_report: Optional[CoverageReport]) -> str:
    """One-line ``Coverage: <category> — <summary>`` for the gate details.

    Returns empty string when no report is attached, so callers can treat
    the line as additive (issue #452). Uses ``.value`` so the persisted
    string is the bare category name (e.g. ``ENTRY_CONDITION_NEVER_TRUE``)
    rather than the Python enum repr, matching the existing ``Category:``
    line style produced by the diagnostics envelope.
    """
    if coverage_report is None:
        return ""
    summary = coverage_report.summary or "(no summary)"
    return f"Coverage: {coverage_report.coverage_category.value} — {summary}"


class BacktestAnomalyDetector(GateResultsMixin):
    """Flag backtest results that are statistically implausible or likely buggy."""

    GATE: ClassVar[str] = GATE

    def check(
        self,
        metrics: BacktestResult,
        trades: List[TradeRecord],
        *,
        mode: str = "backtest",
        dsr_aware: bool = False,
        diagnostics: Optional[BacktestExecutionDiagnostics] = None,
        coverage_report: Optional[CoverageReport] = None,
        phase: StrategyLabPhase = "synthesis",
    ) -> List[QualityGateResult]:
        """Run anomaly checks.

        ``mode="backtest"`` (default) runs the full gate set; ``mode="paper"``
        relaxes gates that assume a multi-year backtest window so short
        paper-trading sessions don't false-trigger on "too few trades".

        ``dsr_aware`` (default False) is set by the Strategy Lab orchestrator
        when walk-forward + ``AcceptanceGate`` is wired in: the OOS Deflated
        Sharpe Ratio is then the authoritative overfitting check, so the
        ``Sharpe > 5.0`` single-window flag is downgraded from critical to
        warning — it still surfaces in the gate result list but no longer
        forces a refinement-loop rewrite when the OOS DSR clears the gate.

        ``diagnostics`` (default None) is the optional execution-path
        envelope produced by the trading service (see issue #404). When
        provided on a zero-trade backtest it enriches the gate result with
        a deterministic failure category and order counters so the
        refinement agent can target the actual failure mode. Other gates
        ignore it.

        ``coverage_report`` (default None, issue #452) is the optional
        deterministic rule-coverage probe output produced by the orchestrator
        (issue #451) on zero/low-trade runs. When provided it adds a single
        ``Coverage: <category> — <summary>`` line to the zero-trade gate
        details so the persisted ``QualityGateResult`` carries the static
        probe's verdict alongside the executor's view. Other gates ignore it.
        """
        self._set_phase(phase)
        results: List[QualityGateResult] = []

        # 1. Zero trades (always flagged — even in paper mode a non-trading
        # strategy is a hard failure).
        if not trades:
            results.append(
                self._critical(_format_zero_trade_details(diagnostics, coverage_report))
            )
            return results

        # 2. Too few trades — backtest-only: paper sessions run over short
        # windows (a few weeks at most) so a <5-trade minimum is
        # inappropriate.  The signals_per_bar floor in BacktestConfig
        # is the paper-mode equivalent.
        if mode != "paper" and len(trades) < 5:
            results.append(
                self._critical(f"Only {len(trades)} trades — statistically meaningless for a multi-year backtest.")
            )

        # 3. Annualized return > 200%
        if metrics.annualized_return_pct > 200:
            results.append(
                self._critical(f"Annualized return {metrics.annualized_return_pct:.1f}% is suspiciously high (>200%) — likely a data or logic bug.")
            )

        # 4. Win rate thresholds
        if metrics.win_rate_pct > 95:
            results.append(
                self._critical(f"Win rate {metrics.win_rate_pct:.1f}% exceeds 95% — almost certainly overfitting or lookahead bias.")
            )
        elif metrics.win_rate_pct > 90:
            results.append(
                self._warning(f"Win rate {metrics.win_rate_pct:.1f}% exceeds 90% — review for possible overfitting.")
            )

        # 5. Extreme profit factor
        if metrics.profit_factor > 10:
            results.append(
                self._critical(f"Profit factor {metrics.profit_factor:.1f} exceeds 10 — likely data snooping or bug.")
            )

        # 5b. Sharpe ratio thresholds.
        # Issue #247: when the orchestrator runs walk-forward and invokes
        # ``AcceptanceGate`` on the OOS Deflated Sharpe, that gate is the
        # authoritative overfitting check — so we downgrade the single-window
        # ``Sharpe > 5.0`` flag from critical to warning under ``dsr_aware``
        # to avoid double-rejecting (and to avoid forcing a refinement rewrite
        # on a strategy whose IS Sharpe is high but OOS DSR clears the gate).
        # Without ``dsr_aware`` the orchestrator only sees the single window,
        # so a Sharpe > 5.0 stays critical to trigger refinement.
        if metrics.sharpe_ratio > 5.0:
            details = (
                f"Sharpe ratio {metrics.sharpe_ratio:.2f} exceeds 5.0 — "
                "almost certainly indicates look-ahead bias or a "
                "calculation artifact. "
                + (
                    "AcceptanceGate's OOS Deflated Sharpe is the "
                    "authoritative overfitting check on this run."
                    if dsr_aware
                    else "When walk-forward is available, "
                    "AcceptanceGate's OOS Deflated Sharpe is the more "
                    "precise overfitting check."
                )
            )
            results.append(self._warning(details) if dsr_aware else self._critical(details))
        elif metrics.sharpe_ratio > 3.0:
            results.append(
                self._warning(f"Sharpe ratio {metrics.sharpe_ratio:.2f} exceeds 3.0 — review for overfitting or data snooping.")
            )

        # 6. Average hold time < 1 day → hard fail on daily bars (Phase 2).
        if trades:
            avg_hold = sum(t.hold_days for t in trades) / len(trades)
            if avg_hold < 1:
                results.append(
                    self._critical(f"Average hold time {avg_hold:.1f} days — sub-day holds "
                            "on daily-bar data are a strong indicator of look-ahead "
                            "bias or intra-bar execution that cannot be replicated live.")
                )

        # 7. Single trade concentration
        if trades:
            total_pnl = sum(abs(t.net_pnl) for t in trades)
            if total_pnl > 0:
                max_single = max(abs(t.net_pnl) for t in trades)
                if max_single / total_pnl > 0.5:
                    results.append(
                        self._warning(f"Largest single trade is {max_single / total_pnl:.0%} of total absolute P&L — high concentration risk.")
                    )

        # 8. All trades identical direction and symbol
        if len(trades) > 1:
            sides = {t.side for t in trades}
            symbols = {t.symbol for t in trades}
            if len(sides) == 1 and len(symbols) == 1:
                results.append(
                    self._warning(f"All {len(trades)} trades are {next(iter(sides))} on {next(iter(symbols))} — no diversification.")
                )

        # 9. Cost sensitivity — edge consumed by transaction costs
        if trades:
            gross_wins = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
            gross_losses = abs(sum(t.gross_pnl for t in trades if t.gross_pnl <= 0))
            gross_pf = gross_wins / gross_losses if gross_losses > 0 else 0.0
            if gross_pf > 1.0 and metrics.profit_factor < 1.0:
                results.append(
                    self._warning(f"Profit factor drops from {gross_pf:.2f} (gross) to "
                            f"{metrics.profit_factor:.2f} (net) — strategy edge is consumed by transaction costs.")
                )

        if not results:
            results.append(
                self._info("Backtest results passed all anomaly checks.")
            )

        return results
