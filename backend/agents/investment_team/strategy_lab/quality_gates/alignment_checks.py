"""Deterministic trade-alignment gate.

Runs inside the orchestrator's trade-alignment loop, replacing the LLM
audit's "read the ledger and write prose" inner step. Each executed
``TradeRecord`` is run through eight deterministic checks against the
structured :class:`StrategySpec`:

  1. Universe — ``trade.symbol in spec.target_symbols``
  2. Side — ``trade.side`` matches an entry rule's declared side
  3. Sizing — ``trade.position_value`` within ±1% of the sizing formula
  4. Stop-loss compliance — return floor respected (or engine-closed)
  5. Take-profit compliance — return ceiling respected (or engine-closed)
  6. Time-stop compliance — guarded no-op until the DSL grows the rule
  7. Entry-signal correlation — predicate(s) evaluate ``True`` on the
     entry bar; near-misses (within
     ``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT``) optionally route to a
     narrow LLM adjudicator.
  8. Signal-exit correlation — when the spec carries
     :class:`SignalExitRule` and the engine did not attribute the
     close to a structured exit, at least one signal-exit predicate
     must evaluate ``True`` at the exit bar.

Results are emitted as per-rule :class:`AlignmentFinding`s for the
record-level ``BacktestRecord.alignment_findings`` field and as
:class:`QualityGateResult` rows for ``quality_gate_results``. The gate
itself does no I/O — ``market_data`` is the same in-memory
``Dict[str, List[OHLCVBar]]`` the orchestrator already fetched.

The re-execution loop is preserved upstream. When this gate reports
``aligned=False``, the orchestrator hands findings to
:meth:`TradeAlignmentAgent.propose_code_fix`, which returns a narrow
code-only patch; the orchestrator then re-executes and re-checks on the
next iteration, up to ``MAX_ALIGNMENT_ROUNDS`` times.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, ClassVar, Dict, List, Optional, Protocol

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ..alignment_findings import AlignmentFinding, NearMissVerdict, Severity
from ..executor import indicators as ind
from ..spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    VolatilityTargetSizing,
)
from .models import GateResultsMixin, QualityGateResult

logger = logging.getLogger(__name__)

GATE = "trade_alignment"

# Sizing tolerance is a literal ±1% requirement from the issue.
_SIZING_TOL = 0.01

_DEFAULT_NEAR_MISS_PCT = 0.01

# The execution engine uses a ``signal-on-T / fill-on-T+1`` contract:
# the strategy sees bar T and submits an order; the engine fills on bar
# T+1; the resulting ``TradeRecord.entry_date`` / ``TradeRecord.exit_date``
# record the FILL bar (T+1), not the signal bar (T). Verified against
# ``backend/agents/investment_team/trading_service/engine/fill_simulator.py``
# where ``Position.entry_timestamp`` and the emitted ``TradeRecord`` are
# both stamped from the bar that fills the order. The alignment gate
# must therefore evaluate spec predicates at the SIGNAL bar (one before
# the recorded entry/exit date), or transient signals (cross events,
# threshold-touches) will be marked misaligned even when execution was
# correct.
_FILL_DELAY_BARS = 1


# ---------------------------------------------------------------------------
# Adjudicator protocol
# ---------------------------------------------------------------------------


class NearMissAdjudicator(Protocol):
    """Callable interface the deterministic gate uses to consult the LLM.

    Implemented by :class:`TradeAlignmentAgent.adjudicate_near_miss`.
    Lifted to a Protocol so the gate has no import dependency on the
    agent module (the agent imports the gate's models).
    """

    def __call__(
        self,
        *,
        rule_id: str,
        predicate_repr: str,
        computed_value: float,
        threshold: float,
        symbol: str,
        entry_date: str,
    ) -> NearMissVerdict: ...


# ---------------------------------------------------------------------------
# Tolerance helpers
# ---------------------------------------------------------------------------


def _near_miss_pct() -> float:
    """Resolve ``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT``.

    Pre: env value, when set, parses as a finite non-negative float.
    Post: returns a non-negative float. ``0`` disables the LLM
    adjudicator (any predicate miss is a hard fail). Default ``0.01``
    = 1% relative tolerance.
    """
    raw = os.environ.get("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT")
    if not raw:
        return _DEFAULT_NEAR_MISS_PCT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT=%r; using default %.4f",
            raw,
            _DEFAULT_NEAR_MISS_PCT,
        )
        return _DEFAULT_NEAR_MISS_PCT
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT=%r is out of range; using default %.4f",
            raw,
            _DEFAULT_NEAR_MISS_PCT,
        )
        return _DEFAULT_NEAR_MISS_PCT
    return value


def _relative_miss(computed: float, threshold: float) -> float:
    """Relative magnitude of the gap between ``computed`` and ``threshold``.

    Returns ``|computed - threshold| / max(|threshold|, |computed|, 1e-12)``.
    The denominator floor of ``1e-12`` prevents division-by-zero when
    both sides are zero (in which case the miss is also zero and the
    ratio is well-defined).
    """
    denom = max(abs(threshold), abs(computed), 1e-12)
    return abs(computed - threshold) / denom


# ---------------------------------------------------------------------------
# Indicator evaluation
# ---------------------------------------------------------------------------


def _bars_to_frame(bars: List[Any]) -> pd.DataFrame:
    """Convert ``List[OHLCVBar]`` to a ``DataFrame`` indexed by date.

    Returns a DataFrame with columns ``open/high/low/close/volume`` and
    a string-typed date index. Order is preserved from the input list.
    """
    rows = [
        {
            "date": b.date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.set_index("date")
    return df


def _select_source_series(df: pd.DataFrame, source: str) -> pd.Series:
    """Return the input series the indicator should read from."""
    if source == "hl2":
        return (df["high"] + df["low"]) / 2.0
    if source == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    return df[source]


def _evaluate_indicator(ref: IndicatorRef, df: pd.DataFrame) -> pd.Series:
    """Recompute an :class:`IndicatorRef` on the given OHLCV frame.

    Pre: ``df`` has the columns the indicator needs and at least enough
    rows for the indicator's warmup. Insufficient rows return a Series
    full of ``NaN`` (pandas' natural rolling-window behaviour); the
    caller treats NaN at the entry bar as "indicator not computable yet".
    """
    name = ref.name
    if name == "sma":
        series = _select_source_series(df, ref.source)
        return ind.sma(series, int(ref.param("period")))
    if name == "ema":
        series = _select_source_series(df, ref.source)
        return ind.ema(series, int(ref.param("period")))
    if name == "rsi":
        series = _select_source_series(df, ref.source)
        return ind.rsi(series, int(ref.param("period")))
    if name == "macd":
        series = _select_source_series(df, ref.source)
        macd_line, signal_line, hist = ind.macd(
            series,
            fast=int(ref.param("fast")),
            slow=int(ref.param("slow")),
            signal=int(ref.param("signal")),
        )
        output = ref.param("output")
        if output == "signal":
            return signal_line
        if output == "histogram":
            return hist
        return macd_line
    if name == "bollinger":
        series = _select_source_series(df, ref.source)
        upper, middle, lower = ind.bollinger_bands(
            series,
            period=int(ref.param("period")),
            num_std=float(ref.param("num_std")),
        )
        band = ref.param("band")
        if band == "upper":
            return upper
        if band == "lower":
            return lower
        return middle
    if name == "atr":
        return ind.atr(df["high"], df["low"], df["close"], period=int(ref.param("period")))
    if name == "adx":
        return ind.adx(df["high"], df["low"], df["close"], period=int(ref.param("period")))
    if name == "stochastic":
        pct_k, pct_d = ind.stochastic(
            df["high"],
            df["low"],
            df["close"],
            k_period=int(ref.param("k_period")),
            d_period=int(ref.param("d_period")),
        )
        return pct_d if ref.param("output") == "d" else pct_k
    if name == "vwap":
        return ind.vwap(df["high"], df["low"], df["close"], df["volume"])
    raise ValueError(f"unknown indicator name: {name!r}")


def _resolve_side_value(
    side: Any,
    df: pd.DataFrame,
    entry_idx: int,
    indicator_cache: Dict[str, pd.Series],
) -> Optional[float]:
    """Resolve one side of a predicate to a scalar at ``entry_idx``.

    Returns ``None`` when the indicator value is ``NaN`` at the entry
    bar (warmup not yet satisfied). ``float`` literals and ``bar.*``
    references are always resolvable.
    """
    if isinstance(side, IndicatorRef):
        key = side.model_dump_json()
        if key not in indicator_cache:
            indicator_cache[key] = _evaluate_indicator(side, df)
        series = indicator_cache[key]
        if entry_idx >= len(series):
            return None
        value = series.iloc[entry_idx]
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)
    if isinstance(side, str):
        if side == "bar.close":
            return float(df["close"].iloc[entry_idx])
        if side == "bar.high":
            return float(df["high"].iloc[entry_idx])
        if side == "bar.low":
            return float(df["low"].iloc[entry_idx])
        if side == "bar.volume":
            return float(df["volume"].iloc[entry_idx])
        raise ValueError(f"unexpected bar-ref string: {side!r}")
    if isinstance(side, (int, float)) and not isinstance(side, bool):
        return float(side)
    raise TypeError(f"unsupported predicate side type: {type(side).__name__}")


def _compare(
    op: str,
    lhs: float,
    rhs: float,
    *,
    prev_lhs: Optional[float] = None,
    prev_rhs: Optional[float] = None,
) -> bool:
    """Evaluate a comparison op on two scalars.

    ``cross_above`` / ``cross_below`` are state transitions: the
    previous bar must have been on or below (resp. on or above) the
    threshold AND the current bar must be strictly above (resp.
    below) it. Collapsing them to ``>`` / ``<`` at the entry bar
    alone would mark any sustained-above strategy as having "crossed
    above" on every bar, letting non-cross strategies wave through
    the gate. When the previous-bar values are not available (e.g.
    entry is the first bar in market_data, or warmup left an
    indicator NaN), the cross is treated as not satisfied —
    deterministic-fail-closed.
    """
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "==":
        return math.isclose(lhs, rhs, rel_tol=1e-9, abs_tol=1e-12)
    if op == "cross_above":
        if prev_lhs is None or prev_rhs is None:
            return False
        return prev_lhs <= prev_rhs and lhs > rhs
    if op == "cross_below":
        if prev_lhs is None or prev_rhs is None:
            return False
        return prev_lhs >= prev_rhs and lhs < rhs
    raise ValueError(f"unknown comparison op: {op!r}")


def _format_predicate(p: Predicate) -> str:
    """Render a predicate as ``lhs op rhs`` for human-readable details."""
    return f"{p.lhs!r} {p.op} {p.rhs!r}"


# ---------------------------------------------------------------------------
# Sizing helpers
# ---------------------------------------------------------------------------


def _expected_position_value(
    sizing: Any,
    *,
    equity_at_entry: float,
    entry_price: float,
) -> Optional[float]:
    """Compute the spec-implied position-value (USD) at trade entry.

    Pre: ``equity_at_entry > 0`` and ``entry_price > 0``.
    Post: returns the expected position value, or ``None`` when the
    sizing variant is not numerically resolvable from trade-level data
    (e.g. :class:`VolatilityTargetSizing`, which needs realized
    portfolio volatility the gate doesn't compute).
    """
    if isinstance(sizing, FixedFractionSizing):
        return float(sizing.fraction) * float(equity_at_entry)
    if isinstance(sizing, FixedNotionalSizing):
        return float(sizing.notional_usd)
    if isinstance(sizing, VolatilityTargetSizing):
        # Vol-target sizing requires the realized volatility stream the
        # engine computed at entry time. The alignment gate doesn't have
        # that signal, so this variant is not numerically checkable
        # here — emit a soft skip rather than a false positive.
        return None
    return None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class DeterministicAlignmentChecker(GateResultsMixin):
    """Run the seven deterministic alignment checks over a trade ledger.

    Contract:
      Pre: every input is a settled artifact from the synthesis loop —
        ``spec`` is the immutable post-ideation spec, ``trades`` is the
        completed ledger, ``market_data`` is the OHLCV dictionary the
        orchestrator passed to the sandbox.
      Post: :meth:`check` returns an :class:`AlignmentCheckResult` whose
        ``aligned`` flag is the conjunction of every critical-severity
        finding's ``passed`` value, and whose ``findings`` /
        ``gate_results`` lists are aligned 1:1 by index.
      Invariants: no I/O; no LLM call unless a near-miss adjudicator is
        supplied AND a check #7 predicate misses within the near-miss
        tolerance.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        *,
        spec: Any,
        trades: List[Any],
        market_data: Dict[str, List[Any]],
        initial_capital: float,
        near_miss_adjudicator: Optional[NearMissAdjudicator] = None,
    ) -> "AlignmentCheckResult":
        """Run every check across every trade.

        Pre: ``initial_capital > 0``; ``market_data`` is keyed by symbol
        and contains the bars used by the backtest that produced
        ``trades``. ``near_miss_adjudicator`` is consulted only when
        :func:`_near_miss_pct` is non-zero and a check #7 predicate misses
        within tolerance.
        Post: returned :class:`AlignmentCheckResult` carries one or more
        :class:`AlignmentFinding`s per trade and matching
        :class:`QualityGateResult` rows. ``aligned`` is ``True`` iff
        every ``severity="critical"`` finding ``passed``.
        """
        assert initial_capital > 0, "initial_capital must be positive"

        with self._using_phase("verification"):
            findings: List[AlignmentFinding] = []
            gate_results: List[QualityGateResult] = []

            # Pre-compute the per-symbol OHLCV frame once per gate run —
            # repeated indicator evaluation across trades on the same
            # symbol then reuses the same DataFrame.
            frames: Dict[str, pd.DataFrame] = {
                symbol: _bars_to_frame(bars) for symbol, bars in market_data.items()
            }
            indicator_caches: Dict[str, Dict[str, pd.Series]] = {
                symbol: {} for symbol in market_data
            }

            # Entry-equity tracker for sizing check #3. Each trade's
            # expected position value is computed against the equity
            # *realized* at the moment of entry — i.e. ``initial_capital
            # + sum(prior.net_pnl for prior in trades if prior.exit_date
            # <= current.entry_date)``. A naive left-fold over net_pnl
            # in entry-date order would leak future PnL from still-open
            # overlapping trades into earlier entries (trade A entering
            # before trade B but exiting after B's entry must not
            # contribute its PnL to B's equity baseline).
            entry_equity_by_trade: Dict[int, float] = {}
            initial = float(initial_capital)
            for trade in trades:
                realized_pnl = sum(
                    float(prior.net_pnl)
                    for prior in trades
                    if prior.exit_date <= trade.entry_date and prior.trade_num != trade.trade_num
                )
                entry_equity_by_trade[trade.trade_num] = initial + realized_pnl

            if not trades:
                # No trades to check — the orchestrator will treat this
                # as aligned vacuously; the upstream "zero trades"
                # critical path is already covered by other gates.
                info = self._info(
                    "No trades to align: deterministic alignment is "
                    "vacuously satisfied. Zero-trade handling is owned "
                    "by upstream gates.",
                    rule_id=None,
                )
                gate_results.append(info)
                return AlignmentCheckResult(
                    aligned=True,
                    findings=findings,
                    gate_results=gate_results,
                    rationale="No trades produced — alignment vacuously satisfied.",
                )

            near_miss_pct = _near_miss_pct()

            for trade in trades:
                # The seven checks. Each yields zero-or-more findings
                # (and matching gate_result rows). Critical failures
                # drive ``aligned=False``.
                self._check_universe(spec, trade, findings, gate_results)
                self._check_side(spec, trade, findings, gate_results)
                self._check_sizing(
                    spec,
                    trade,
                    findings,
                    gate_results,
                    entry_equity=entry_equity_by_trade[trade.trade_num],
                )
                self._check_stop_loss(spec, trade, findings, gate_results)
                self._check_take_profit(spec, trade, findings, gate_results)
                self._check_time_stop(trade, findings, gate_results)
                self._check_entry_signal(
                    spec,
                    trade,
                    frames,
                    indicator_caches,
                    near_miss_pct,
                    near_miss_adjudicator,
                    findings,
                    gate_results,
                )
                self._check_signal_exit(
                    spec,
                    trade,
                    frames,
                    indicator_caches,
                    findings,
                    gate_results,
                )

            aligned = all(f.passed for f in findings if f.severity == "critical")
            rationale = (
                "All deterministic alignment checks passed."
                if aligned
                else "One or more critical alignment findings."
            )
            return AlignmentCheckResult(
                aligned=aligned,
                findings=findings,
                gate_results=gate_results,
                rationale=rationale,
            )

    # ------------------------------------------------------------------
    # Check 1 — universe
    # ------------------------------------------------------------------
    def _check_universe(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        target = list(getattr(spec, "target_symbols", []) or [])
        if not target:
            # Spec didn't pin a universe — alignment defers to the
            # asset-class fallback; no finding emitted.
            return
        passed = trade.symbol in target
        details = (
            f"Trade #{trade.trade_num} symbol {trade.symbol!r} ∈ target_symbols={target}."
            if passed
            else (
                f"Trade #{trade.trade_num} symbol {trade.symbol!r} is not in "
                f"spec.target_symbols={target}."
            )
        )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="universe",
            check_name="universe",
            passed=passed,
            severity="info" if passed else "critical",
            details=details,
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Check 2 — side matches an entry rule's declared side
    # ------------------------------------------------------------------
    def _check_side(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        entry_rules = [
            r for r in (getattr(spec, "entry_rules", []) or []) if isinstance(r, EntryRule)
        ]
        if not entry_rules:
            return
        allowed_sides = sorted({r.side for r in entry_rules})
        passed = trade.side in allowed_sides
        details = (
            f"Trade #{trade.trade_num} side {trade.side!r} matches an entry "
            f"rule (allowed sides: {allowed_sides})."
            if passed
            else (
                f"Trade #{trade.trade_num} side {trade.side!r} is not declared "
                f"by any entry rule (allowed sides: {allowed_sides})."
            )
        )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="entry:side",
            check_name="side",
            passed=passed,
            severity="info" if passed else "critical",
            details=details,
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Check 3 — sizing within ±1%
    # ------------------------------------------------------------------
    def _check_sizing(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
        *,
        entry_equity: float,
    ) -> None:
        sizing = getattr(spec, "sizing", None)
        if sizing is None:
            return

        expected = _expected_position_value(
            sizing,
            equity_at_entry=entry_equity,
            entry_price=float(trade.entry_price),
        )
        if expected is None:
            # Vol-target / unknown sizing — emit info skip so the row
            # exists in the audit ledger but doesn't gate the verdict.
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="sizing",
                check_name="sizing",
                passed=True,
                severity="info",
                details=(
                    f"Sizing variant {type(sizing).__name__} is not numerically "
                    "checkable at trade-level; deferring to engine-side enforcement."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        actual = float(trade.position_value)
        rel_miss = _relative_miss(actual, expected)
        within_tol = rel_miss <= _SIZING_TOL

        if within_tol:
            severity: Severity = "info"
            passed = True
            details = (
                f"Trade #{trade.trade_num} position_value=${actual:,.2f} within "
                f"±{_SIZING_TOL * 100:.1f}% of expected ${expected:,.2f}."
            )
        else:
            # ``TradeRecord`` documents ``participation_clipped`` /
            # ``partial_fill_count`` as ``None`` when the engine hasn't
            # annotated the trade — distinct from "annotated as no
            # caveat". Treating unknown as known-false would mark every
            # legacy / pre-annotation trade with sizing drift as
            # critical and trigger needless alignment-fix iterations.
            # Three-way classification:
            #   - explicit caveat present (clipped=True or partial>0)
            #     → info (engine-attested explanation)
            #   - explicit no-caveat (clipped=False AND partial==0)
            #     → critical (engine attested execution was clean,
            #     so the drift is a real misalignment)
            #   - unknown (either field is None) → warning (flag the
            #     drift but don't claim it's definitively misaligned)
            explicit_caveat = bool(trade.participation_clipped) or (
                trade.partial_fill_count is not None and trade.partial_fill_count > 0
            )
            explicit_no_caveat = (
                trade.participation_clipped is False and trade.partial_fill_count == 0
            )
            if explicit_caveat:
                severity = "info"
                passed = True
                caveat_msg = ""
                if trade.participation_clipped:
                    caveat_msg = " (participation_clipped=True)"
                elif trade.partial_fill_count is not None and trade.partial_fill_count > 0:
                    caveat_msg = f" (partial_fill_count={trade.partial_fill_count})"
            elif explicit_no_caveat:
                severity = "critical"
                passed = False
                caveat_msg = " (no execution caveat reported)"
            else:
                # Unknown execution metadata — downgrade to warning.
                severity = "warning"
                passed = False
                caveat_msg = " (execution metadata not annotated; unknown)"
            details = (
                f"Trade #{trade.trade_num} position_value=${actual:,.2f} differs "
                f"from expected ${expected:,.2f} by "
                f"{rel_miss * 100:.2f}% (tolerance ±{_SIZING_TOL * 100:.1f}%)"
                f"{caveat_msg}."
            )

        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="sizing",
            check_name="sizing",
            passed=passed,
            severity=severity,
            details=details,
            computed_value=actual,
            expected_value=expected,
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Check 4 — stop-loss compliance
    # ------------------------------------------------------------------
    def _check_stop_loss(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        stop_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, StopLossRule)
        ]
        if not stop_rules:
            return

        # Trailing-high / trailing-low stops are path-dependent: the
        # effective floor moves with the running peak/trough between
        # entry and exit, so it cannot be validated from
        # ``trade.return_pct`` alone. Emit an info-severity skip for
        # each trailing rule so the audit ledger is self-describing,
        # and only run the entry-price floor check against rules whose
        # ``basis == "entry_price"``.
        entry_basis_rules = [r for r in stop_rules if r.basis == "entry_price"]
        trailing_rules = [r for r in stop_rules if r.basis != "entry_price"]
        for tr in trailing_rules:
            findings.append(
                AlignmentFinding(
                    trade_num=trade.trade_num,
                    rule_id=f"exit:stop_loss:{tr.basis}",
                    check_name="stop_loss",
                    passed=True,
                    severity="info",
                    details=(
                        f"Trade #{trade.trade_num} stop_loss basis={tr.basis!r} is "
                        "path-dependent (depends on running peak/trough); deferring "
                        "to engine-side enforcement."
                    ),
                )
            )
            gate_results.append(self._emit_for_finding(findings[-1]))

        if not entry_basis_rules:
            return

        # Tightest entry-price stop wins — if the spec has multiple
        # entry-basis stops, the floor is the smallest ``pct``.
        tightest = min(r.pct for r in entry_basis_rules)
        # Engine-attributed stop-loss exit is the strongest signal that
        # the engine honoured the rule, regardless of what
        # ``return_pct`` rounded to.
        if trade.exit_reason == "engine_exit:stop_loss":
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:stop_loss",
                check_name="stop_loss",
                passed=True,
                severity="info",
                details=(f"Trade #{trade.trade_num} closed by engine stop-loss attribution."),
                computed_value=float(trade.return_pct),
                expected_value=-tightest * 100.0,
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        # Account for transaction costs + slippage when validating the
        # observed return against the spec's stop-loss floor. A 5% stop
        # on a strategy with 5 bps cost + 2 bps slippage realistically
        # bottoms at -5.07%, not -5.00%.
        return_pct = float(trade.return_pct)
        floor_pct = -tightest * 100.0
        # Allow a small absolute slack (0.5 percentage point) on top of
        # the strict floor to absorb cost/slippage drift without forcing
        # the gate to know the exact fee model.
        slack = 0.5
        passed = return_pct >= floor_pct - slack
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="exit:stop_loss",
            check_name="stop_loss",
            passed=passed,
            severity="info" if passed else "critical",
            details=(
                f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% within "
                f"stop-loss floor {floor_pct:.2f}% (±{slack:.2f}pp slack)."
                if passed
                else (
                    f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% breaches "
                    f"stop-loss floor {floor_pct:.2f}% (±{slack:.2f}pp slack) and "
                    "exit_reason is not engine_exit:stop_loss."
                )
            ),
            computed_value=return_pct,
            expected_value=floor_pct,
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Check 5 — take-profit compliance
    # ------------------------------------------------------------------
    def _check_take_profit(
        self,
        spec: Any,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        tp_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, TakeProfitRule)
        ]
        if not tp_rules:
            return
        # Tightest take-profit wins for ceiling computation. A trade
        # closed at a higher return than spec's tightest TP either was
        # engine-fired with attribution, or the engine missed the rule.
        tightest = min(r.pct for r in tp_rules)
        if trade.exit_reason == "engine_exit:take_profit":
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:take_profit",
                check_name="take_profit",
                passed=True,
                severity="info",
                details=(f"Trade #{trade.trade_num} closed by engine take-profit attribution."),
                computed_value=float(trade.return_pct),
                expected_value=tightest * 100.0,
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        return_pct = float(trade.return_pct)
        ceiling_pct = tightest * 100.0
        # Symmetry with stop-loss: trades may close slightly past the
        # spec ceiling via gap-up. The check is "did the engine ever
        # let a position carry materially past the take-profit ceiling
        # without engine attribution?". 0.5pp absolute slack is the
        # same forgiveness we extend to the stop-loss floor.
        slack = 0.5
        passed = return_pct <= ceiling_pct + slack
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="exit:take_profit",
            check_name="take_profit",
            passed=passed,
            severity="info" if passed else "critical",
            details=(
                f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% within "
                f"take-profit ceiling {ceiling_pct:.2f}% (±{slack:.2f}pp slack)."
                if passed
                else (
                    f"Trade #{trade.trade_num} return_pct={return_pct:.2f}% exceeds "
                    f"take-profit ceiling {ceiling_pct:.2f}% (±{slack:.2f}pp slack) "
                    "and exit_reason is not engine_exit:take_profit."
                )
            ),
            computed_value=return_pct,
            expected_value=ceiling_pct,
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Check 6 — time-stop compliance (guarded no-op until the DSL grows it)
    # ------------------------------------------------------------------
    def _check_time_stop(
        self,
        trade: Any,
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        # ``TimeStopRule`` is intentionally not part of the current spec
        # DSL (``spec_dsl.py`` excludes bar-counting time stops). The
        # check is wired so it activates the moment the DSL adds the
        # rule; today it emits a single ``info`` row per trade so the
        # ledger is self-describing.
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="exit:time_stop",
            check_name="time_stop",
            passed=True,
            severity="info",
            details="Time-stop check is a no-op (TimeStopRule not in current DSL).",
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Check 7 — entry-signal correlation (with near-miss adjudication)
    # ------------------------------------------------------------------
    def _check_entry_signal(
        self,
        spec: Any,
        trade: Any,
        frames: Dict[str, pd.DataFrame],
        indicator_caches: Dict[str, Dict[str, pd.Series]],
        near_miss_pct: float,
        adjudicator: Optional[NearMissAdjudicator],
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        entry_rules = [
            r for r in (getattr(spec, "entry_rules", []) or []) if isinstance(r, EntryRule)
        ]
        if not entry_rules:
            return

        # Restrict to entry rules whose side matches the trade's side,
        # preserving each rule's original index in ``spec.entry_rules``
        # so ``rule_id`` values like ``entry[2]`` map back to the spec
        # the operator actually wrote. Renumbering relative to the
        # filtered subset would silently misattribute findings on the
        # second side when a spec mixes long/short rules.
        matching = [(orig_idx, r) for orig_idx, r in enumerate(entry_rules) if r.side == trade.side]
        if not matching:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:side_mismatch",
                check_name="entry_signal",
                passed=False,
                severity="info",
                details=(
                    f"Trade #{trade.trade_num}: no entry rule with side={trade.side!r}; "
                    "check #2 (side) carries the critical finding."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        df = frames.get(trade.symbol)
        if df is None or df.empty:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:bars_missing",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: no market_data bars for symbol "
                    f"{trade.symbol!r}. Cannot reproduce entry signal."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        matching_positions = np.where(df.index.to_numpy() == trade.entry_date)[0]
        if matching_positions.size == 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:bar_missing",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: entry_date {trade.entry_date!r} is "
                    f"not present in market_data for {trade.symbol!r}."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return
        # Duplicate dates are vanishingly rare in real backtest data —
        # if they exist, the first occurrence is the one the engine
        # would have fired on.
        fill_idx = int(matching_positions[0])

        # Shift to the SIGNAL bar (engine fills the next bar after the
        # signal fires; see ``_FILL_DELAY_BARS`` constant). When the
        # fill bar is the first bar in market_data we have no signal
        # bar to verify — fall closed (warmup-style) rather than mark
        # the trade as critical for lack of data.
        signal_idx = fill_idx - _FILL_DELAY_BARS
        if signal_idx < 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="entry:signal_bar_missing",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: fill bar {trade.entry_date!r} "
                    "is the first bar in market_data; the signal bar (one "
                    "before) is not present, cannot reproduce entry signal."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        cache = indicator_caches.setdefault(trade.symbol, {})

        # Track the closest near-miss across all matching rules. We
        # want to consult the adjudicator at most once per trade —
        # picking the tightest (smallest relative miss) gives the LLM
        # the most-likely-legitimate candidate to rule on.
        rule_evaluations: List[
            Dict[str, Any]
        ] = []  # one entry per matching rule, with eval outcome.

        any_satisfied = False
        for orig_idx, rule in matching:
            outcome = self._evaluate_entry_rule_predicate(
                rule, df, signal_idx, cache, rule_idx=orig_idx
            )
            rule_evaluations.append(outcome)
            if outcome["status"] == "satisfied":
                any_satisfied = True

        if any_satisfied:
            # At least one matching entry predicate fires — the entry
            # is aligned. Emit a single info finding citing the
            # satisfied rule.
            satisfied = next(o for o in rule_evaluations if o["status"] == "satisfied")
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id=satisfied["rule_id"],
                check_name="entry_signal",
                passed=True,
                severity="info",
                details=(
                    f"Trade #{trade.trade_num} entry satisfied by "
                    f"{satisfied['rule_id']}: {satisfied['predicate_repr']} → "
                    f"lhs={satisfied['lhs']:.6g}, rhs={satisfied['rhs']:.6g}."
                ),
                computed_value=satisfied.get("lhs"),
                expected_value=satisfied.get("rhs"),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        # No matching rule was satisfied. Find the tightest near-miss
        # among the evaluations; if it's within tolerance and the
        # adjudicator is wired up, consult the LLM. Otherwise emit
        # critical.
        near_miss_candidates = [
            o for o in rule_evaluations if o["status"] == "miss" and o["rel_miss"] is not None
        ]
        if near_miss_candidates and near_miss_pct > 0 and adjudicator is not None:
            tightest = min(near_miss_candidates, key=lambda o: o["rel_miss"])
            if tightest["rel_miss"] <= near_miss_pct:
                verdict = self._consult_near_miss(adjudicator, tightest, trade)
                finding = AlignmentFinding(
                    trade_num=trade.trade_num,
                    rule_id=tightest["rule_id"],
                    check_name="entry_signal",
                    passed=verdict.legitimate,
                    severity="info" if verdict.legitimate else "critical",
                    details=(
                        f"Trade #{trade.trade_num} entry near-miss "
                        f"({tightest['rel_miss'] * 100:.2f}% relative) on "
                        f"{tightest['rule_id']}: {tightest['predicate_repr']}. "
                        f"Adjudicator: {verdict.rationale!s}"
                    ),
                    computed_value=tightest["lhs"],
                    expected_value=tightest["rhs"],
                )
                findings.append(finding)
                gate_results.append(self._emit_for_finding(finding))
                return

        # Hard miss — emit critical citing the first matching rule that
        # produced a real evaluation (priority: misses with values,
        # then warmup misses).
        primary = next(
            (o for o in rule_evaluations if o["status"] == "miss"),
            rule_evaluations[0],
        )
        if primary["status"] == "warmup":
            details = (
                f"Trade #{trade.trade_num} entry on {trade.entry_date} hit a "
                f"warmup-NaN indicator value for {primary['rule_id']}: "
                f"{primary['predicate_repr']}. The engine fired before its "
                "indicator was ready."
            )
        else:
            details = (
                f"Trade #{trade.trade_num} entry predicate not satisfied for "
                f"{primary['rule_id']}: {primary['predicate_repr']} "
                f"(lhs={primary['lhs']:.6g}, rhs={primary['rhs']:.6g})."
            )
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id=primary["rule_id"],
            check_name="entry_signal",
            passed=False,
            severity="critical",
            details=details,
            computed_value=primary.get("lhs"),
            expected_value=primary.get("rhs"),
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    # ------------------------------------------------------------------
    # Check 8 — signal-exit correlation
    # ------------------------------------------------------------------
    def _check_signal_exit(
        self,
        spec: Any,
        trade: Any,
        frames: Dict[str, pd.DataFrame],
        indicator_caches: Dict[str, Dict[str, pd.Series]],
        findings: List[AlignmentFinding],
        gate_results: List[QualityGateResult],
    ) -> None:
        """Validate that strategy-emitted closes match a SignalExitRule.

        Pre: ``frames`` is the per-symbol OHLCV map; ``indicator_caches``
        is the per-trade cache the entry-signal check populates.
        Post: emits zero-or-more findings:
          - no SignalExitRule in spec → no-op (other exit checks cover
            stop_loss / take_profit / time_stop)
          - engine-attributed close (``exit_reason`` starts with
            ``engine_exit:``) → info skip (signal-exit check N/A)
          - strategy-emitted close (or unknown attribution) without
            any SignalExitRule predicate firing at the exit bar →
            critical
          - first SignalExitRule whose predicate fires → info pass
        """
        signal_exit_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, SignalExitRule)
        ]
        if not signal_exit_rules:
            return

        exit_reason = getattr(trade, "exit_reason", None) or ""
        if exit_reason.startswith("engine_exit:"):
            # Engine attribution already covered by the matching
            # structured-exit check (stop_loss / take_profit /
            # time_stop). Emit a single info row so the audit ledger
            # records that the signal-exit check was reached and
            # deliberately skipped.
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit",
                check_name="signal_exit",
                passed=True,
                severity="info",
                details=(
                    f"Trade #{trade.trade_num} closed via engine "
                    f"attribution {exit_reason!r}; signal-exit check N/A."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        df = frames.get(trade.symbol)
        if df is None or df.empty:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit:bars_missing",
                check_name="signal_exit",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: no market_data bars for "
                    f"{trade.symbol!r}. Cannot reproduce signal exit."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        matching_positions = np.where(df.index.to_numpy() == trade.exit_date)[0]
        if matching_positions.size == 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit:bar_missing",
                check_name="signal_exit",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: exit_date {trade.exit_date!r} "
                    f"is not present in market_data for {trade.symbol!r}."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        fill_idx = int(matching_positions[0])

        # Shift to the SIGNAL bar — same engine contract as the entry
        # check (``_FILL_DELAY_BARS``). ``trade.exit_date`` is the fill
        # bar where the close completed; the strategy's signal-exit
        # predicate fired one bar earlier.
        signal_idx = fill_idx - _FILL_DELAY_BARS
        if signal_idx < 0:
            finding = AlignmentFinding(
                trade_num=trade.trade_num,
                rule_id="exit:signal_exit:signal_bar_missing",
                check_name="signal_exit",
                passed=False,
                severity="critical",
                details=(
                    f"Trade #{trade.trade_num}: fill bar {trade.exit_date!r} "
                    "is the first bar in market_data; the signal bar (one "
                    "before) is not present, cannot reproduce signal exit."
                ),
            )
            findings.append(finding)
            gate_results.append(self._emit_for_finding(finding))
            return

        cache = indicator_caches.setdefault(trade.symbol, {})

        # Try each signal-exit rule in spec order — the first one whose
        # predicate fires at the signal bar wins the alignment.
        for rule_idx, rule in enumerate(signal_exit_rules):
            try:
                lhs_value = _resolve_side_value(rule.when.lhs, df, signal_idx, cache)
                rhs_value = _resolve_side_value(rule.when.rhs, df, signal_idx, cache)
                prev_lhs: Optional[float] = None
                prev_rhs: Optional[float] = None
                if rule.when.op in ("cross_above", "cross_below") and signal_idx > 0:
                    prev_lhs = _resolve_side_value(rule.when.lhs, df, signal_idx - 1, cache)
                    prev_rhs = _resolve_side_value(rule.when.rhs, df, signal_idx - 1, cache)
            except (ValueError, TypeError):
                continue

            if lhs_value is None or rhs_value is None:
                continue  # warmup NaN at exit bar — try next rule

            if _compare(
                rule.when.op,
                lhs_value,
                rhs_value,
                prev_lhs=prev_lhs,
                prev_rhs=prev_rhs,
            ):
                finding = AlignmentFinding(
                    trade_num=trade.trade_num,
                    rule_id=f"exit:signal_exit[{rule_idx}]",
                    check_name="signal_exit",
                    passed=True,
                    severity="info",
                    details=(
                        f"Trade #{trade.trade_num} signal-exit satisfied by "
                        f"exit[{rule_idx}]: {_format_predicate(rule.when)} → "
                        f"lhs={lhs_value:.6g}, rhs={rhs_value:.6g}."
                    ),
                    computed_value=lhs_value,
                    expected_value=rhs_value,
                )
                findings.append(finding)
                gate_results.append(self._emit_for_finding(finding))
                return

        # No SignalExitRule fired at the exit bar, but the engine did
        # not attribute the close to a structured rule — the strategy
        # closed without a matching signal.
        finding = AlignmentFinding(
            trade_num=trade.trade_num,
            rule_id="exit:signal_exit",
            check_name="signal_exit",
            passed=False,
            severity="critical",
            details=(
                f"Trade #{trade.trade_num} exited on {trade.exit_date} without "
                "engine attribution, but no SignalExitRule predicate fires at "
                "the exit bar."
            ),
        )
        findings.append(finding)
        gate_results.append(self._emit_for_finding(finding))

    def _evaluate_entry_rule_predicate(
        self,
        rule: EntryRule,
        df: pd.DataFrame,
        entry_idx: int,
        cache: Dict[str, pd.Series],
        *,
        rule_idx: int,
    ) -> Dict[str, Any]:
        """Evaluate one entry rule's predicate at the SIGNAL bar.

        Pre: the caller has already shifted from the fill bar (where
        ``TradeRecord.entry_date`` points) back to the signal bar
        (``signal_idx = fill_idx - _FILL_DELAY_BARS``); the
        ``entry_idx`` parameter here is the resolved signal bar.

        Returns a dict with:
          - ``status``: ``"satisfied"`` | ``"miss"`` | ``"warmup"``
          - ``rule_id``: ``f"entry[{rule_idx}]"``
          - ``predicate_repr``: rendered predicate string
          - ``lhs`` / ``rhs``: resolved scalars (``None`` for warmup)
          - ``rel_miss``: relative miss when ``status == "miss"`` and
            ``rhs`` is a numeric anchor; ``None`` otherwise.
        """
        rule_id = f"entry[{rule_idx}]"
        predicate_repr = _format_predicate(rule.when)
        op = rule.when.op
        try:
            lhs_value = _resolve_side_value(rule.when.lhs, df, entry_idx, cache)
            rhs_value = _resolve_side_value(rule.when.rhs, df, entry_idx, cache)
            # Cross ops need previous-bar state to distinguish a real
            # state transition from a sustained inequality. Resolving
            # the previous bar adds one extra series lookup per side;
            # the cache hits on the indicator path so the cost is the
            # ``iloc[entry_idx - 1]`` access, not a full recompute.
            prev_lhs: Optional[float] = None
            prev_rhs: Optional[float] = None
            if op in ("cross_above", "cross_below") and entry_idx > 0:
                prev_lhs = _resolve_side_value(rule.when.lhs, df, entry_idx - 1, cache)
                prev_rhs = _resolve_side_value(rule.when.rhs, df, entry_idx - 1, cache)
        except (ValueError, TypeError) as exc:
            return {
                "status": "warmup",
                "rule_id": rule_id,
                "predicate_repr": predicate_repr,
                "lhs": None,
                "rhs": None,
                "rel_miss": None,
                "_error": str(exc),
            }

        if lhs_value is None or rhs_value is None:
            return {
                "status": "warmup",
                "rule_id": rule_id,
                "predicate_repr": predicate_repr,
                "lhs": None,
                "rhs": None,
                "rel_miss": None,
            }

        # For cross ops we additionally need the previous-bar values
        # to evaluate. ``entry_idx == 0`` or a NaN warmup on the prior
        # bar produces ``None`` previous values; ``_compare`` then
        # treats the cross as not satisfied, which downstream becomes
        # a normal "miss" finding (the gate falls closed on
        # indeterminate crosses rather than fabricating a satisfied
        # outcome).
        is_cross = op in ("cross_above", "cross_below")
        if is_cross and (prev_lhs is None or prev_rhs is None) and entry_idx == 0:
            return {
                "status": "warmup",
                "rule_id": rule_id,
                "predicate_repr": predicate_repr,
                "lhs": lhs_value,
                "rhs": rhs_value,
                "rel_miss": None,
            }

        satisfied = _compare(
            op,
            lhs_value,
            rhs_value,
            prev_lhs=prev_lhs,
            prev_rhs=prev_rhs,
        )
        if satisfied:
            return {
                "status": "satisfied",
                "rule_id": rule_id,
                "predicate_repr": predicate_repr,
                "lhs": lhs_value,
                "rhs": rhs_value,
                "rel_miss": 0.0,
            }
        return {
            "status": "miss",
            "rule_id": rule_id,
            "predicate_repr": predicate_repr,
            "lhs": lhs_value,
            "rhs": rhs_value,
            # Cross-predicate misses are about a missing prior-bar
            # transition, not a numerical gap on the current bar. A
            # sustained-above strategy can present a tiny
            # ``|curr_lhs - curr_rhs|`` and the LLM near-miss
            # adjudicator would happily legitimize it even though no
            # cross happened — bypass the near-miss path entirely for
            # cross ops by emitting ``rel_miss=None``. The aggregation
            # step filters near-miss candidates on ``rel_miss is not
            # None``.
            "rel_miss": None if is_cross else _relative_miss(lhs_value, rhs_value),
        }

    def _consult_near_miss(
        self,
        adjudicator: NearMissAdjudicator,
        evaluation: Dict[str, Any],
        trade: Any,
    ) -> NearMissVerdict:
        """Invoke the LLM near-miss adjudicator with a single tight prompt.

        Failures route to a deterministic fail-closed verdict
        (``legitimate=False``) rather than propagating — the orchestrator
        is then free to treat the trade as misaligned and rebuild via
        ``propose_code_fix`` on the next iteration.
        """
        try:
            return adjudicator(
                rule_id=evaluation["rule_id"],
                predicate_repr=evaluation["predicate_repr"],
                computed_value=float(evaluation["lhs"]),
                threshold=float(evaluation["rhs"]),
                symbol=trade.symbol,
                entry_date=trade.entry_date,
            )
        except Exception as exc:  # pragma: no cover — fail-closed safety net
            logger.warning("Near-miss adjudicator raised; failing closed: %s", exc)
            return NearMissVerdict(
                legitimate=False,
                rationale=f"adjudicator error: {type(exc).__name__}",
            )

    # ------------------------------------------------------------------
    # Per-finding gate-row helper
    # ------------------------------------------------------------------
    def _emit_for_finding(self, finding: AlignmentFinding) -> QualityGateResult:
        """Translate one :class:`AlignmentFinding` into a
        :class:`QualityGateResult` row for the gate-result stream.

        The per-finding row uses ``gate_name="alignment_finding"`` —
        deliberately distinct from the gate's ``GATE="trade_alignment"``
        cycle-level aggregate. A misaligned cycle with N trades × 7
        checks can emit dozens of per-finding rows, and
        :class:`ConvergenceTracker.record` increments
        ``_failure_modes[gate_name]`` per failed row. Sharing the
        ``trade_alignment`` name would inflate the cycle-level failure
        count by the per-finding fan-out and prematurely trip
        ``get_failure_directives(min_occurrences=3)`` after a single
        bad cycle. The orchestrator builds a separate aggregate
        ``trade_alignment`` row per cycle that the tracker keys on
        instead.
        """
        # ``_using_phase`` is active because callers wrap the public
        # ``check()`` body in ``with self._using_phase("verification")``;
        # mirror its assertion here for the same fail-fast contract.
        assert self._phase is not None, (
            f"{type(self).__name__}._emit_for_finding must be inside `with self._using_phase(...)`"
        )
        details = f"[{finding.check_name}] {finding.details}"
        return QualityGateResult(
            gate_name="alignment_finding",
            phase=self._phase,
            passed=finding.passed,
            severity=finding.severity,
            details=details,
            rule_id=finding.rule_id,
        )


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


class AlignmentCheckResult(BaseModel):
    """Aggregated output of :meth:`DeterministicAlignmentChecker.check`."""

    aligned: bool
    findings: List[AlignmentFinding] = Field(default_factory=list)
    gate_results: List[QualityGateResult] = Field(default_factory=list)
    rationale: str = ""
