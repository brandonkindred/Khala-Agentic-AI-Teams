"""Threshold-based anomaly detector for backtest results."""

from __future__ import annotations

from typing import ClassVar, Iterable, List, Optional

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

    When ``diagnostics`` carries a deterministic ``zero_trade_category``,
    surface the category, the executor's summary, the order counters, and any
    rejection-reason histogram. Falls back to the historical generic message
    when diagnostics are missing or the executor couldn't classify the
    failure. When ``coverage_report`` is also present, append a one-line
    ``Coverage: <category> — <summary>`` so the persisted gate result records
    the deterministic rule-coverage verdict alongside the executor's view.
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
    if coverage_report is None:
        return ""
    summary = coverage_report.summary or "(no summary)"
    return f"Coverage: {coverage_report.coverage_category.value} — {summary}"


class BacktestAnomalyDetector(GateResultsMixin):
    """Flag backtest results that are statistically implausible or likely buggy.

    Contract: every call to :meth:`check` returns a non-empty
    ``List[QualityGateResult]``. Every result carries the caller's ``phase``
    and ``gate_name == GATE``. Rules are listed in ``_RULES`` and iterated in
    order; the zero-trade short-circuit fires before any other rule because a
    no-trade backtest invalidates every downstream statistic.
    """

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
        """Run anomaly checks and tag every result with ``phase``.

        Pre: ``metrics`` is a BacktestResult; ``trades`` is a list.
        Post: results non-empty; every entry carries the caller's ``phase``.

        ``mode="backtest"`` (default) runs the full gate set; ``mode="paper"``
        relaxes gates that assume a multi-year backtest window so short
        paper-trading sessions don't false-trigger on "too few trades".

        ``dsr_aware`` (default False) is set by the Strategy Lab orchestrator
        when walk-forward + ``AcceptanceGate`` is wired in: the OOS Deflated
        Sharpe Ratio is then the authoritative overfitting check, so the
        ``Sharpe > 5.0`` single-window flag is downgraded from critical to
        warning.

        ``diagnostics`` and ``coverage_report`` enrich the zero-trade gate
        result with a deterministic failure category and order counters.
        Other rules ignore them.
        """
        self._set_phase(phase)
        # Store call-scoped context so each rule reads from instance state
        # without an explicit-arg parade.
        self._metrics = metrics
        self._trades = trades
        self._mode = mode
        self._dsr_aware = dsr_aware
        self._diagnostics = diagnostics
        self._coverage_report = coverage_report
        try:
            # Zero trades is a hard short-circuit — every downstream statistic
            # is meaningless without trades, so we return immediately.
            if not trades:
                return [
                    self._critical(_format_zero_trade_details(diagnostics, coverage_report))
                ]
            results = [r for rule in self._RULES for r in rule(self)]
            return results or [self._info("Backtest results passed all anomaly checks.")]
        finally:
            self._metrics = None  # type: ignore[assignment]
            self._trades = None  # type: ignore[assignment]
            self._diagnostics = None
            self._coverage_report = None

    # ------------------------------------------------------------------
    # Rules — each reads call-scoped state from ``self`` and yields zero or
    # more results. Listed in ``_RULES`` below.
    # ------------------------------------------------------------------
    def _check_trade_count_floor(self) -> Iterable[QualityGateResult]:
        # Paper sessions run over short windows (a few weeks at most) so a
        # <5-trade minimum is inappropriate; the signals_per_bar floor is the
        # paper-mode equivalent.
        if self._mode != "paper" and len(self._trades) < 5:
            return (
                self._critical(
                    f"Only {len(self._trades)} trades — "
                    "statistically meaningless for a multi-year backtest."
                ),
            )
        return ()

    def _check_annualized_return_ceiling(self) -> Iterable[QualityGateResult]:
        if self._metrics.annualized_return_pct > 200:
            return (
                self._critical(
                    f"Annualized return {self._metrics.annualized_return_pct:.1f}% is "
                    "suspiciously high (>200%) — likely a data or logic bug."
                ),
            )
        return ()

    def _check_win_rate(self) -> Iterable[QualityGateResult]:
        wr = self._metrics.win_rate_pct
        if wr > 95:
            return (
                self._critical(
                    f"Win rate {wr:.1f}% exceeds 95% — almost certainly overfitting "
                    "or lookahead bias."
                ),
            )
        if wr > 90:
            return (
                self._warning(
                    f"Win rate {wr:.1f}% exceeds 90% — review for possible overfitting."
                ),
            )
        return ()

    def _check_profit_factor(self) -> Iterable[QualityGateResult]:
        if self._metrics.profit_factor > 10:
            return (
                self._critical(
                    f"Profit factor {self._metrics.profit_factor:.1f} exceeds 10 — "
                    "likely data snooping or bug."
                ),
            )
        return ()

    def _check_sharpe_ratio(self) -> Iterable[QualityGateResult]:
        # When the orchestrator runs walk-forward + AcceptanceGate, OOS
        # Deflated Sharpe is the authoritative overfitting check — so the
        # single-window ``Sharpe > 5.0`` flag is downgraded from critical to
        # warning to avoid double-rejecting strategies whose IS Sharpe is
        # high but OOS DSR clears.
        sr = self._metrics.sharpe_ratio
        if sr > 5.0:
            details = (
                f"Sharpe ratio {sr:.2f} exceeds 5.0 — almost certainly indicates "
                "look-ahead bias or a calculation artifact. "
                + (
                    "AcceptanceGate's OOS Deflated Sharpe is the authoritative "
                    "overfitting check on this run."
                    if self._dsr_aware
                    else "When walk-forward is available, AcceptanceGate's OOS "
                    "Deflated Sharpe is the more precise overfitting check."
                )
            )
            return (self._warning(details) if self._dsr_aware else self._critical(details),)
        if sr > 3.0:
            return (
                self._warning(
                    f"Sharpe ratio {sr:.2f} exceeds 3.0 — review for overfitting "
                    "or data snooping."
                ),
            )
        return ()

    def _check_avg_hold_time(self) -> Iterable[QualityGateResult]:
        avg_hold = sum(t.hold_days for t in self._trades) / len(self._trades)
        if avg_hold < 1:
            return (
                self._critical(
                    f"Average hold time {avg_hold:.1f} days — sub-day holds on "
                    "daily-bar data are a strong indicator of look-ahead bias or "
                    "intra-bar execution that cannot be replicated live."
                ),
            )
        return ()

    def _check_trade_concentration(self) -> Iterable[QualityGateResult]:
        total_pnl = sum(abs(t.net_pnl) for t in self._trades)
        if total_pnl <= 0:
            return ()
        max_single = max(abs(t.net_pnl) for t in self._trades)
        if max_single / total_pnl > 0.5:
            return (
                self._warning(
                    f"Largest single trade is {max_single / total_pnl:.0%} of total "
                    "absolute P&L — high concentration risk."
                ),
            )
        return ()

    def _check_trade_diversification(self) -> Iterable[QualityGateResult]:
        if len(self._trades) <= 1:
            return ()
        sides = {t.side for t in self._trades}
        symbols = {t.symbol for t in self._trades}
        if len(sides) == 1 and len(symbols) == 1:
            return (
                self._warning(
                    f"All {len(self._trades)} trades are {next(iter(sides))} on "
                    f"{next(iter(symbols))} — no diversification."
                ),
            )
        return ()

    def _check_cost_sensitivity(self) -> Iterable[QualityGateResult]:
        gross_wins = sum(t.gross_pnl for t in self._trades if t.gross_pnl > 0)
        gross_losses = abs(sum(t.gross_pnl for t in self._trades if t.gross_pnl <= 0))
        gross_pf = gross_wins / gross_losses if gross_losses > 0 else 0.0
        if gross_pf > 1.0 and self._metrics.profit_factor < 1.0:
            return (
                self._warning(
                    f"Profit factor drops from {gross_pf:.2f} (gross) to "
                    f"{self._metrics.profit_factor:.2f} (net) — strategy edge is "
                    "consumed by transaction costs."
                ),
            )
        return ()

    # Rules iterated in order by ``check``. Adding a rule is a one-line edit.
    _RULES: ClassVar[tuple] = (
        _check_trade_count_floor,
        _check_annualized_return_ceiling,
        _check_win_rate,
        _check_profit_factor,
        _check_sharpe_ratio,
        _check_avg_hold_time,
        _check_trade_concentration,
        _check_trade_diversification,
        _check_cost_sensitivity,
    )
