"""Threshold-based anomaly detector for backtest results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar, Dict, Iterable, List, Optional

from ...market_data_service import OHLCVBar
from ...models import BacktestExecutionDiagnostics, BacktestResult, CoverageReport, TradeRecord
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase


@dataclass(frozen=True)
class BacktestAnomalyCtx:
    """Per-``check`` context handed to every rule in ``BacktestAnomalyDetector._RULES``.

    Frozen so individual rules cannot accidentally mutate state visible to
    later rules. Built once at the top of ``check`` and threaded through each
    rule call — replaces the previous ``self._<attr>`` pattern that risked
    bleed-over across concurrent ``check`` invocations.
    """

    metrics: BacktestResult
    trades: List[TradeRecord]
    mode: str
    dsr_aware: bool
    diagnostics: Optional[BacktestExecutionDiagnostics]
    coverage_report: Optional[CoverageReport]
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    timeframe: Optional[str] = None
    market_data: Optional[Dict[str, List[OHLCVBar]]] = None


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
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        timeframe: Optional[str] = None,
        market_data: Optional[Dict[str, List[OHLCVBar]]] = None,
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

        ``start_date`` / ``end_date`` / ``timeframe`` are required by the
        frequency-aware trade-count gate. When omitted the gate falls back
        to the legacy ``< 5 trades`` floor so callers that don't yet wire
        them through keep their previous behaviour.

        ``market_data`` is required by the look-ahead pattern gate (needs
        entry-bar OHLC). When omitted the gate emits no result.
        """
        with self._using_phase(phase):
            # Zero trades is a hard short-circuit — every downstream statistic
            # is meaningless without trades, so we return immediately.
            if not trades:
                return [self._critical(_format_zero_trade_details(diagnostics, coverage_report))]
            ctx = BacktestAnomalyCtx(
                metrics=metrics,
                trades=trades,
                mode=mode,
                dsr_aware=dsr_aware,
                diagnostics=diagnostics,
                coverage_report=coverage_report,
                start_date=start_date,
                end_date=end_date,
                timeframe=timeframe,
                market_data=market_data,
            )
            results = [r for rule in self._RULES for r in rule(self, ctx)]
            return results or [self._info("Backtest results passed all anomaly checks.")]

    # ------------------------------------------------------------------
    # Rules — each takes the per-call ``BacktestAnomalyCtx`` and yields zero
    # or more results. Listed in ``_RULES`` below.
    # ------------------------------------------------------------------
    def _check_trade_count_floor(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        """Frequency-aware trade-count adequacy gate.

        Preconditions:
          - ``ctx.trades`` is non-empty (the ``check`` zero-trade short-circuit
            handles the empty case before this rule fires).
        Postconditions:
          - Returns an empty tuple in ``paper`` mode (legacy behaviour).
          - When ``start_date`` / ``end_date`` / ``timeframe`` are missing,
            falls back to the legacy ``< 5 trades → critical`` floor so
            callers that haven't yet wired the new kwargs keep their prior
            verdicts.
          - When all three are supplied, the expected trade count is
            ``window_days / expected_hold_days``: realised count below
            ``25 %`` of expected → critical, ``25–50 %`` → warning, above
            50 % → pass.
        Invariants:
          - Never returns more than one result.
        """
        if ctx.mode == "paper":
            return ()

        observed = len(ctx.trades)
        expected = _expected_trade_count(
            start_date=ctx.start_date,
            end_date=ctx.end_date,
            timeframe=ctx.timeframe,
            trades=ctx.trades,
        )
        if expected is None:
            # Legacy fallback: callers that don't yet pass dates/timeframe
            # still get the original floor so behaviour is unchanged.
            if observed < 5:
                return (
                    self._critical(
                        f"Only {observed} trades — "
                        "statistically meaningless for a multi-year backtest."
                    ),
                )
            return ()

        ratio = observed / expected if expected > 0 else 0.0
        if ratio < 0.25:
            return (
                self._critical(
                    f"Observed {observed} trades vs ~{expected} expected "
                    f"({ratio:.0%} of expected) given window and holding period — "
                    "well below the 25% floor for a meaningful sample."
                ),
            )
        if ratio < 0.50:
            return (
                self._warning(
                    f"Observed {observed} trades vs ~{expected} expected "
                    f"({ratio:.0%} of expected) — review whether the signal "
                    "is firing as designed."
                ),
            )
        return ()

    def _check_annualized_return_ceiling(
        self, ctx: BacktestAnomalyCtx
    ) -> Iterable[QualityGateResult]:
        if ctx.metrics.annualized_return_pct > 200:
            return (
                self._critical(
                    f"Annualized return {ctx.metrics.annualized_return_pct:.1f}% is "
                    "suspiciously high (>200%) — likely a data or logic bug."
                ),
            )
        return ()

    def _check_win_rate(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        wr = ctx.metrics.win_rate_pct
        if wr > 95:
            return (
                self._critical(
                    f"Win rate {wr:.1f}% exceeds 95% — almost certainly overfitting "
                    "or lookahead bias."
                ),
            )
        if wr > 90:
            return (
                self._warning(f"Win rate {wr:.1f}% exceeds 90% — review for possible overfitting."),
            )
        return ()

    def _check_profit_factor(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        if ctx.metrics.profit_factor > 10:
            return (
                self._critical(
                    f"Profit factor {ctx.metrics.profit_factor:.1f} exceeds 10 — "
                    "likely data snooping or bug."
                ),
            )
        return ()

    def _check_sharpe_ratio(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        # When the orchestrator runs walk-forward + AcceptanceGate, OOS
        # Deflated Sharpe is the authoritative overfitting check — so the
        # single-window ``Sharpe > 5.0`` flag is downgraded from critical to
        # warning to avoid double-rejecting strategies whose IS Sharpe is
        # high but OOS DSR clears.
        sr = ctx.metrics.sharpe_ratio
        if sr > 5.0:
            details = (
                f"Sharpe ratio {sr:.2f} exceeds 5.0 — almost certainly indicates "
                "look-ahead bias or a calculation artifact. "
                + (
                    "AcceptanceGate's OOS Deflated Sharpe is the authoritative "
                    "overfitting check on this run."
                    if ctx.dsr_aware
                    else "When walk-forward is available, AcceptanceGate's OOS "
                    "Deflated Sharpe is the more precise overfitting check."
                )
            )
            return (self._warning(details) if ctx.dsr_aware else self._critical(details),)
        if sr > 3.0:
            return (
                self._warning(
                    f"Sharpe ratio {sr:.2f} exceeds 3.0 — review for overfitting or data snooping."
                ),
            )
        return ()

    def _check_avg_hold_time(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        avg_hold = sum(t.hold_days for t in ctx.trades) / len(ctx.trades)
        if avg_hold < 1:
            return (
                self._critical(
                    f"Average hold time {avg_hold:.1f} days — sub-day holds on "
                    "daily-bar data are a strong indicator of look-ahead bias or "
                    "intra-bar execution that cannot be replicated live."
                ),
            )
        return ()

    def _check_trade_concentration(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        total_pnl = sum(abs(t.net_pnl) for t in ctx.trades)
        if total_pnl <= 0:
            return ()
        max_single = max(abs(t.net_pnl) for t in ctx.trades)
        if max_single / total_pnl > 0.5:
            return (
                self._warning(
                    f"Largest single trade is {max_single / total_pnl:.0%} of total "
                    "absolute P&L — high concentration risk."
                ),
            )
        return ()

    def _check_trade_diversification(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        if len(ctx.trades) <= 1:
            return ()
        sides = {t.side for t in ctx.trades}
        symbols = {t.symbol for t in ctx.trades}
        if len(sides) == 1 and len(symbols) == 1:
            return (
                self._warning(
                    f"All {len(ctx.trades)} trades are {next(iter(sides))} on "
                    f"{next(iter(symbols))} — no diversification."
                ),
            )
        return ()

    def _check_lookahead_bar_predictability(
        self, ctx: BacktestAnomalyCtx
    ) -> Iterable[QualityGateResult]:
        """Flag trades whose sign agrees suspiciously well with the entry bar's
        intrabar direction — a heuristic for look-ahead bias that AST checks
        miss.

        Preconditions:
          - ``ctx.trades`` is non-empty.
          - ``ctx.market_data`` is provided; otherwise the rule emits nothing
            (the synthesis-loop call site doesn't always have it in scope).
        Postconditions:
          - Returns at most one result.
          - Critical when ``n_eligible >= 20`` and agreement rate ``>= 95%``,
            OR when ``n_eligible < 20`` and agreement is perfect across at
            least 5 trades (smaller-sample backstop).
          - Warning when ``n_eligible >= 20`` and ``80% <= agreement < 95%``.
        Invariants:
          - Entry-bar lookup is an EXACT timestamp match against
            ``OHLCVBar.date`` — calendar-prefix matching would pick the
            first bar of the day on intraday timeframes and make the
            agreement statistic arbitrary. Trades whose entry timestamp
            doesn't exact-match any bar are skipped (ineligible).
          - Trades whose entry bar has ``close == open`` or whose return
            is exactly zero contribute no sign signal and are skipped.
          - Degenerate samples (all eligible bars move one direction, or
            all eligible trades return one sign) are skipped — the rate
            would be uninformative.
        """
        if ctx.market_data is None or ctx.mode == "paper":
            return ()
        agreements = 0
        eligible = 0
        bar_dirs: set[int] = set()
        ret_dirs: set[int] = set()
        for trade in ctx.trades:
            bars = ctx.market_data.get(trade.symbol)
            if not bars:
                continue
            entry_bar = _find_bar_by_timestamp(bars, trade.entry_date)
            if entry_bar is None:
                continue
            bar_dir = _sign(entry_bar.close - entry_bar.open)
            ret_dir = _sign(trade.return_pct)
            if bar_dir == 0 or ret_dir == 0:
                continue
            eligible += 1
            bar_dirs.add(bar_dir)
            ret_dirs.add(ret_dir)
            if bar_dir == ret_dir:
                agreements += 1
        if eligible == 0:
            return ()
        # Degenerate samples (every eligible bar moves the same direction, or
        # every eligible trade returns the same sign) make the agreement
        # statistic uninformative — a fixture where every bar is positive
        # and every trade is a winner trivially scores 100% without any
        # look-ahead. Require both sides of the sign distribution before
        # promoting agreement to a finding.
        if len(bar_dirs) < 2 or len(ret_dirs) < 2:
            return ()
        rate = agreements / eligible
        if eligible >= 20 and rate >= 0.95:
            return (
                self._critical(
                    f"Entry-bar direction agrees with trade return on "
                    f"{agreements}/{eligible} trades ({rate:.0%}) — perfectly "
                    "predictable from the entry bar's close-minus-open indicates "
                    "intrabar look-ahead bias."
                ),
            )
        if eligible < 20 and eligible >= 5 and rate >= 0.999:
            return (
                self._critical(
                    f"Entry-bar direction matches trade return on every "
                    f"eligible trade ({agreements}/{eligible}) — perfect "
                    "agreement at small sample is consistent with intrabar "
                    "look-ahead bias; collect more trades to disambiguate."
                ),
            )
        if eligible >= 20 and rate >= 0.80:
            return (
                self._warning(
                    f"Entry-bar direction agrees with trade return on "
                    f"{agreements}/{eligible} trades ({rate:.0%}) — review the "
                    "entry-signal computation for subtle look-ahead."
                ),
            )
        return ()

    def _check_cost_sensitivity(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        gross_wins = sum(t.gross_pnl for t in ctx.trades if t.gross_pnl > 0)
        gross_losses = abs(sum(t.gross_pnl for t in ctx.trades if t.gross_pnl <= 0))
        gross_pf = gross_wins / gross_losses if gross_losses > 0 else 0.0
        if gross_pf > 1.0 and ctx.metrics.profit_factor < 1.0:
            return (
                self._warning(
                    f"Profit factor drops from {gross_pf:.2f} (gross) to "
                    f"{ctx.metrics.profit_factor:.2f} (net) — strategy edge is "
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
        _check_lookahead_bar_predictability,
        _check_cost_sensitivity,
    )


# ──────────────────────────────────────────────────────────────────────────
# Module-level helpers used by the rules above.
# ──────────────────────────────────────────────────────────────────────────


# Default expected holding period in CALENDAR DAYS per spec timeframe.
# Daily-bar swing strategies hold roughly two weeks; intraday holds collapse
# to fractions of a day. These defaults are the fallback when neither the
# average observed hold nor a structured TimeStopRule supplies a value.
_DEFAULT_EXPECTED_HOLD_DAYS: Dict[str, float] = {
    "1d": 10.0,
    "1h": 0.5,
    "15m": 0.1,
    "5m": 0.04,
    "1m": 0.01,
}


def _expected_trade_count(
    *,
    start_date: Optional[str],
    end_date: Optional[str],
    timeframe: Optional[str],
    trades: List[TradeRecord],
) -> Optional[int]:
    """Estimate the expected number of trades over the backtest window.

    Preconditions:
      - ``trades`` is non-empty (callers skip the empty-ledger case before
        invoking this helper).
    Postconditions:
      - Returns ``None`` whenever any required input is missing or unparseable
        — the gate then falls back to its legacy floor.
      - Returns ``max(1, round(window_days / expected_hold_days))`` when the
        inputs are usable. ``expected_hold_days`` is the average observed
        hold when at least one trade reports ``hold_days > 0``, otherwise the
        per-timeframe default.
    Invariants:
      - Never returns a value ``<= 0``.
    """
    if not start_date or not end_date or not timeframe:
        return None
    window_days = _window_days(start_date, end_date)
    if window_days is None or window_days <= 0:
        return None

    observed_holds = [t.hold_days for t in trades if t.hold_days and t.hold_days > 0]
    if observed_holds:
        expected_hold = sum(observed_holds) / len(observed_holds)
    else:
        expected_hold = _DEFAULT_EXPECTED_HOLD_DAYS.get(timeframe)
    if not expected_hold or expected_hold <= 0:
        return None

    expected = round(window_days / expected_hold)
    return max(1, expected)


def _window_days(start_date: str, end_date: str) -> Optional[int]:
    start = _parse_iso_date(start_date)
    end = _parse_iso_date(end_date)
    if start is None or end is None:
        return None
    return (end - start).days


def _parse_iso_date(value: str) -> Optional[date]:
    """Parse ``YYYY-MM-DD`` or full ISO datetime strings; returns ``None`` on
    malformed input rather than raising so the gate can fall back gracefully.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(value).date()
        except (TypeError, ValueError):
            return None


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _find_bar_by_timestamp(bars: List[OHLCVBar], target: str) -> Optional[OHLCVBar]:
    """Return the bar whose ``date`` equals ``target`` exactly.

    Preconditions:
      - ``bars`` is iterable; ``target`` is a string.
    Postconditions:
      - Returns ``None`` when ``target`` is empty or no bar's ``date``
        field equals ``target`` exactly.
      - Never falls back to date-prefix matching: on intraday timeframes
        (1m/5m/15m/1h) many bars share a calendar day, so a prefix match
        would pick the first bar of the day rather than the actual entry
        bar and make the look-ahead agreement statistic arbitrary.
        Trades whose entry timestamp can't be resolved this way are
        skipped by the caller (counted as ineligible).
    Invariants:
      - Pure function; no side effects.
    """
    if not target:
        return None
    for bar in bars:
        if bar.date == target:
            return bar
    return None
