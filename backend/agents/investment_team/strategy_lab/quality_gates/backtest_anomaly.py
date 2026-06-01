"""Threshold-based anomaly detector for backtest results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
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

    # Per-trade reductions multiple rules need. Computed once and memoised on
    # the (frozen) ctx so the concentration and cost-sensitivity rules don't
    # each re-walk every trade. ``cached_property`` writes straight into the
    # instance ``__dict__`` — frozen-dataclass ``__setattr__`` is bypassed.
    @cached_property
    def total_abs_pnl(self) -> float:
        """Sum of ``abs(net_pnl)`` over all trades (0.0 when empty)."""
        return sum(abs(t.net_pnl) for t in self.trades)

    @cached_property
    def max_abs_pnl(self) -> float:
        """Largest ``abs(net_pnl)`` over all trades (0.0 when empty)."""
        return max((abs(t.net_pnl) for t in self.trades), default=0.0)

    @cached_property
    def gross_wins(self) -> float:
        """Sum of positive ``gross_pnl`` over all trades."""
        return sum(t.gross_pnl for t in self.trades if t.gross_pnl > 0)

    @cached_property
    def gross_losses(self) -> float:
        """Absolute sum of non-positive ``gross_pnl`` over all trades."""
        return abs(sum(t.gross_pnl for t in self.trades if t.gross_pnl <= 0))


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
        total_pnl = ctx.total_abs_pnl
        if total_pnl <= 0:
            return ()
        max_single = ctx.max_abs_pnl
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
        """Flag trades whose outcome agrees suspiciously well with the
        intrabar direction of their entry and exit bars — a heuristic for
        look-ahead bias that AST checks miss.

        Preconditions:
          - ``ctx.trades`` is non-empty.
          - ``ctx.market_data`` is provided; otherwise the rule emits nothing
            (the synthesis-loop call site doesn't always have it in scope).
        Postconditions:
          - Returns up to two results: at most one combined-rate finding
            (critical / warning), plus an additive degraded-sample warning
            when eligibility is sparse.
          - Emits a single info-level result tagged
            ``sample insufficient`` when no trade resolves a usable bar
            direction (every candidate was filtered by missing market
            data, unresolved entry/exit bars, an unrecognised side, or a
            zero-return signal). The previous behaviour returned nothing,
            so reviewers couldn't tell whether the detector ran or was
            skipped.
          - Critical when ``trades_with_signal >= 20`` and combined
            agreement ``>= 95%``, OR when ``trades_with_signal < 20`` and
            combined agreement is perfect across at least 5 trades
            (smaller-sample backstop). Sample-size thresholds key off
            distinct *trades* with a usable signal, NOT off the doubled
            observation count — two bar observations per trade are
            correlated through the trade's own outcome and don't compose
            into independent samples for the threshold purpose.
          - Warning when ``trades_with_signal >= 20`` and
            ``80% <= combined_agreement < 95%``.
          - Additionally emits an additive warning when
            ``len(ctx.trades) >= 10`` and the entry-bar resolvability
            ratio (``entry_eligible / len(trades)``) drops below 0.5 —
            so reviewers know the agreement statistic was computed from
            a degraded sample.
        Invariants:
          - Trade side is folded into the comparison: profitable shorts in
            this codebase carry ``return_pct > 0`` on DOWN bars, so a naive
            ``bar_dir == sign(return_pct)`` would systematically miss short-
            side look-ahead. The comparison uses ``effective_bar_dir =
            bar_dir * side_sign`` (``side_sign = +1`` for long, ``-1`` for
            short), which models "did the strategy's directional bet on
            the bar pay off". Look-ahead-biased trades — long on up bars
            or short on down bars, both winners — produce agreement; the
            heuristic catches both directions.
          - Entry-bar AND exit-bar directions are folded into a single
            combined rate (``(entry_agreements + exit_agreements) /
            (entry_eligible + exit_eligible)``). A single-rate combination
            has lower variance than a max-of-two-rates and avoids
            double-counting the same leak signal across the two bars.
          - Entry / exit bar lookup prefers an exact
            ``bar.date == trade.entry_date`` match — calendar-prefix
            matching would pick the wrong bar on intraday timeframes and
            make the agreement statistic arbitrary. When exact match fails
            but at least one bar shares the trade's calendar date (the
            production case where the simulator truncates timestamps to
            ``YYYY-MM-DD`` while intraday bars carry full ISO timestamps),
            the rule falls back to the day's net intrabar direction
            (first bar's open → last bar's close).
          - Trades whose target bar has ``close == open``, whose return is
            exactly zero, or whose ``side`` isn't ``long``/``short`` are
            skipped — they contribute no sign signal.
          - Degenerate samples (all effective bar directions move one way,
            or all eligible trades return one sign) are skipped — the rate
            would be uninformative.
        """
        if ctx.market_data is None or ctx.mode == "paper":
            return ()
        entry_agreements = 0
        entry_eligible = 0
        exit_agreements = 0
        exit_eligible = 0
        # Count of distinct trades that contributed at least one
        # observation (entry- OR exit-bar direction resolved). Used for
        # sample-size thresholds because two observations from the same
        # trade are NOT independent — both correlate with the trade's
        # own outcome, so an agreement-rate threshold sized for 20
        # independent samples should not be tripped by 10 trades' worth
        # of paired observations.
        trades_with_signal = 0
        effective_bar_dirs: set[int] = set()
        ret_dirs: set[int] = set()
        for trade in ctx.trades:
            bars = ctx.market_data.get(trade.symbol)
            if not bars:
                continue
            side_sign = _side_sign(trade.side)
            if side_sign is None:
                continue
            ret_dir = _sign(trade.return_pct)
            if ret_dir == 0:
                continue
            contributed = False
            # Entry-bar contribution.
            entry_dir = _resolve_entry_bar_direction(bars, trade.entry_date)
            if entry_dir is not None:
                effective_entry = entry_dir * side_sign
                entry_eligible += 1
                effective_bar_dirs.add(effective_entry)
                ret_dirs.add(ret_dir)
                if effective_entry == ret_dir:
                    entry_agreements += 1
                contributed = True
            # Exit-bar contribution. ``_resolve_entry_bar_direction`` is
            # a generic timestamp→direction resolver; the same exact /
            # day-aggregate semantics apply to the exit bar.
            exit_dir = _resolve_entry_bar_direction(bars, trade.exit_date)
            if exit_dir is not None:
                effective_exit = exit_dir * side_sign
                exit_eligible += 1
                effective_bar_dirs.add(effective_exit)
                ret_dirs.add(ret_dir)
                if effective_exit == ret_dir:
                    exit_agreements += 1
                contributed = True
            if contributed:
                trades_with_signal += 1
        eligible = entry_eligible + exit_eligible
        if eligible == 0:
            return (
                self._info(
                    "lookahead_bar_predictability: 0 eligible observations — "
                    "sample insufficient for the predictability heuristic "
                    "(no trade resolved an entry- or exit-bar direction)."
                ),
            )

        results: List[QualityGateResult] = []
        # Degraded-sample warning fires on the *entry* ratio rather than
        # the combined one because exit-bar resolvability tracks entry
        # resolvability tightly; the entry ratio is the more interpretable
        # signal for "the agreement statistic was computed against a
        # smaller-than-expected fraction of the ledger".
        if len(ctx.trades) >= 10:
            entry_ratio = entry_eligible / len(ctx.trades)
            if entry_ratio < 0.5:
                results.append(
                    self._warning(
                        f"lookahead_bar_predictability ran on a degraded "
                        f"sample: only {entry_eligible}/{len(ctx.trades)} "
                        f"trades had a resolvable entry bar "
                        f"({entry_ratio:.0%} eligibility) — agreement "
                        "statistic below should be interpreted with caution."
                    )
                )

        # Degenerate samples (every effective bar direction is one way, or
        # every eligible trade returns the same sign) make the agreement
        # statistic uninformative — a fixture where every long trade wins on
        # an up bar trivially scores 100% without any look-ahead. Require
        # both sides of the sign distribution before promoting agreement to
        # a finding.
        if len(effective_bar_dirs) < 2 or len(ret_dirs) < 2:
            return tuple(results)
        agreements = entry_agreements + exit_agreements
        rate = agreements / eligible
        if trades_with_signal >= 20 and rate >= 0.95:
            results.append(
                self._critical(
                    f"Entry+exit bar direction agrees with trade return on "
                    f"{agreements}/{eligible} bar observations ({rate:.0%}) "
                    f"across {trades_with_signal} trades — perfectly "
                    "predictable from the close-minus-open of the "
                    "entry/exit bars indicates intrabar look-ahead bias."
                )
            )
            return tuple(results)
        if trades_with_signal < 20 and trades_with_signal >= 5 and rate >= 0.999:
            results.append(
                self._critical(
                    f"Entry+exit bar direction matches trade return on every "
                    f"eligible observation ({agreements}/{eligible}) across "
                    f"{trades_with_signal} trades — perfect agreement at "
                    "small sample is consistent with intrabar look-ahead "
                    "bias; collect more trades to disambiguate."
                )
            )
            return tuple(results)
        if trades_with_signal >= 20 and rate >= 0.80:
            results.append(
                self._warning(
                    f"Entry+exit bar direction agrees with trade return on "
                    f"{agreements}/{eligible} bar observations ({rate:.0%}) "
                    f"across {trades_with_signal} trades — review the entry- "
                    "and exit-signal computation for subtle look-ahead."
                )
            )
            return tuple(results)
        return tuple(results)

    def _check_cost_sensitivity(self, ctx: BacktestAnomalyCtx) -> Iterable[QualityGateResult]:
        gross_pf = ctx.gross_wins / ctx.gross_losses if ctx.gross_losses > 0 else 0.0
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
# average observed hold period supplies a value.
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


def _side_sign(side: str) -> Optional[int]:
    """Map ``"long"``/``"short"`` to ``+1``/``-1`` for look-ahead direction math.

    Postconditions:
      - Case-insensitive match on the canonical labels.
      - Returns ``None`` for any other value (skip the trade) — silently
        defaulting to long would hide short-side look-ahead bias.
    """
    if not isinstance(side, str):
        return None
    lowered = side.strip().lower()
    if lowered == "long":
        return 1
    if lowered == "short":
        return -1
    return None


def _resolve_entry_bar_direction(bars: List[OHLCVBar], target: str) -> Optional[int]:
    """Return the sign of the entry-bar's ``close - open`` for look-ahead
    detection, accommodating the date-only / timestamped asymmetry.

    Preconditions:
      - ``bars`` is iterable over :class:`OHLCVBar`.
      - ``target`` is a non-empty string. Production callers pass
        ``trade.entry_date``, which the trade simulator truncates to
        ``YYYY-MM-DD``; bars may carry either ``YYYY-MM-DD`` (daily
        fetchers today) or ``YYYY-MM-DDTHH:MM:SS`` (any future intraday
        fetcher).
    Postconditions:
      - Returns ``None`` when ``target`` is empty, no bar matches the
        target's calendar date, or the resolved direction is exactly
        zero (uninformative for sign-agreement).
      - Otherwise returns ``+1`` or ``-1`` representing the direction
        of the entry "bar":
          * **Exact match path** — when at least one bar's ``date`` equals
            ``target`` exactly, the direction is ``sign(bar.close - bar.open)``
            of that bar. Daily backtests resolve here.
          * **Day-aggregate fallback** — when no bar exact-matches but at
            least one bar's calendar-date prefix matches ``target[:10]``,
            the direction is ``sign(last_bar.close - first_bar.open)``
            across all same-day bars in chronological order. This is the
            day's net intrabar direction and is a legitimate look-ahead
            probe even when the trade ledger has truncated an intraday
            entry timestamp to a date — silent skipping would otherwise
            let look-ahead slip through the realism veto.
    Invariants:
      - Pure function; never mutates ``bars``.
      - Day-aggregate path only fires when exact match fails — the
        precise-bar signal is preferred whenever it's available.
    """
    if not target:
        return None
    target_date = target[:10]
    same_day: List[OHLCVBar] = []
    for bar in bars:
        if bar.date == target:
            return _sign(bar.close - bar.open) or None
        if bar.date[:10] == target_date:
            same_day.append(bar)
    if not same_day:
        return None
    direction = _sign(same_day[-1].close - same_day[0].open)
    return direction or None
