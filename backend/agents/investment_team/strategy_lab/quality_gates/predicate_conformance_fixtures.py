"""Synthetic-bar fixtures for the predicate conformance shadow check.

Each fixture produces ~50-80 OHLCV bars per ``EntryRule`` / ``SignalExitRule``
that exercise **both** true and false predicate states across the sequence.
Ground truth is computed by running ``evaluate_predicate()`` from the shared
predicate evaluator against every bar — the gate then compares per-bar
strategy ``submit_order`` calls against these verdicts.

Only predicate-bearing rules are in scope: ``StopLossRule`` and
``TakeProfitRule`` are P&L-based and excluded.

Recipes are organised by predicate shape, mirroring the approach in the
rule-probes synthesizer but targeting oscillating series rather than
single-trigger sequences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional

import pandas as pd

from ...market_data_service import OHLCVBar
from ..executor.predicate_evaluator import PandasHistoryView, evaluate_predicate
from ..spec_dsl import EntryRule, IndicatorRef, Predicate, SignalExitRule
from .conformance_bars import (
    _bars_to_df,
    _normalise_ohlc,
    _resolve_probe_symbol,
)

_MIN_FIXTURE_BARS = 60
_OSCILLATION_SEGMENT = 10
_BASE_CLOSE = 100.0
_BASE_VOLUME = 1_000_000.0


@dataclass(frozen=True)
class ConformanceFixture:
    """One rule's synthetic bar sequence with per-bar expected verdicts.

    ``expected_verdicts[i]`` is ``True`` when the engine evaluator says the
    predicate is satisfied at bar ``i``, ``False`` when it is a miss, or
    ``None`` during indicator warmup.  Aligned 1:1 with ``bars``.

    Preconditions:
      - ``rule_kind == "entry"`` implies ``side`` is set.
    Postconditions:
      - ``synthesizable=True`` implies ``bars`` is non-empty and
        ``expected_verdicts`` has the same length as ``bars``.
      - ``synthesizable=True`` implies at least one ``True`` and one
        ``False`` in ``expected_verdicts`` (both states exercised).
    """

    rule_id: str
    rule_kind: Literal["entry", "signal_exit"]
    side: Optional[Literal["long", "short"]] = None
    symbol: str = "PROBE"
    bars: List[OHLCVBar] = field(default_factory=list)
    expected_verdicts: List[Optional[bool]] = field(default_factory=list)
    synthesizable: bool = True
    unsynthesizable_reason: Optional[str] = None


def generate_conformance_fixtures(
    spec: Any, *, compiled_code: str = ""
) -> List[ConformanceFixture]:
    """Build one ``ConformanceFixture`` per predicate-bearing rule in ``spec``.

    Preconditions:
      ``spec`` has ``entry_rules`` and ``exit_rules`` lists.
    Postconditions:
      Returns one fixture per ``EntryRule`` and ``SignalExitRule``.
      ``StopLossRule`` / ``TakeProfitRule`` members are skipped.
    """
    symbol = _resolve_probe_symbol(spec, compiled_code)
    fixtures: List[ConformanceFixture] = []

    for idx, rule in enumerate(getattr(spec, "entry_rules", []) or []):
        if not isinstance(rule, EntryRule):
            continue
        rule_id = f"entry[{idx}]"
        fixture = _build_fixture_for_predicate(
            pred=rule.when,
            rule_id=rule_id,
            rule_kind="entry",
            side=rule.side,
            symbol=symbol,
        )
        fixtures.append(fixture)

    for idx, rule in enumerate(getattr(spec, "exit_rules", []) or []):
        if not isinstance(rule, SignalExitRule):
            continue
        rule_id = f"exit[{idx}]:signal_exit"
        fixture = _build_fixture_for_predicate(
            pred=rule.when,
            rule_id=rule_id,
            rule_kind="signal_exit",
            side=None,
            symbol=symbol,
        )
        fixtures.append(fixture)

    return fixtures


def _build_fixture_for_predicate(
    pred: Predicate,
    rule_id: str,
    rule_kind: Literal["entry", "signal_exit"],
    side: Optional[Literal["long", "short"]],
    symbol: str,
) -> ConformanceFixture:
    """Dispatch to the appropriate fixture recipe based on predicate shape."""
    # Tree predicates (``all_of`` / ``any_of``) are not single-comparison shapes
    # the oscillating-bar recipes can drive true⇄false on one indicator, so they
    # are marked unsynthesizable and the gate skips them. The common
    # multi-confirmation case is compilable (``requires_custom_code=False``) and
    # never reaches this custom-code-only gate; this guard just keeps a tree
    # ``when`` under custom code from crashing on ``pred.lhs``.
    if not isinstance(pred, Predicate):
        return ConformanceFixture(
            rule_id=rule_id,
            rule_kind=rule_kind,
            side=side,
            symbol=symbol,
            synthesizable=False,
            unsynthesizable_reason="tree_predicate",
        )
    bars = _synthesise_oscillating_bars(pred)
    if bars is None:
        return ConformanceFixture(
            rule_id=rule_id,
            rule_kind=rule_kind,
            side=side,
            symbol=symbol,
            synthesizable=False,
            unsynthesizable_reason="no_recipe_for_predicate_shape",
        )

    bars = _stamp_dates_on_bars(bars, symbol)
    verdicts = _compute_verdicts(pred, bars)

    has_true = any(v is True for v in verdicts)
    has_false = any(v is False for v in verdicts)
    if not (has_true and has_false):
        return ConformanceFixture(
            rule_id=rule_id,
            rule_kind=rule_kind,
            side=side,
            symbol=symbol,
            synthesizable=False,
            unsynthesizable_reason="no_predicate_state_change",
        )

    return ConformanceFixture(
        rule_id=rule_id,
        rule_kind=rule_kind,
        side=side,
        symbol=symbol,
        bars=bars,
        expected_verdicts=verdicts,
    )


def _compute_verdicts(pred: Predicate, bars: List[OHLCVBar]) -> List[Optional[bool]]:
    """Run the engine evaluator on every bar and return per-bar verdicts."""
    df = _bars_to_df(bars)
    cache: dict[str, pd.Series] = {}
    view = PandasHistoryView(df, cache)
    verdicts: List[Optional[bool]] = []
    for i in range(len(bars)):
        result = evaluate_predicate(pred, view, i)
        if result.status == "warmup":
            verdicts.append(None)
        elif result.status == "satisfied":
            verdicts.append(True)
        else:
            verdicts.append(False)
    return verdicts


def _stamp_dates_on_bars(bars: List[OHLCVBar], symbol: str) -> List[OHLCVBar]:
    """Assign ascending dates and normalise OHLC to satisfy preflight checks."""
    dates = pd.date_range("2024-01-01", periods=len(bars), freq="D")
    return [
        _normalise_ohlc(
            OHLCVBar(
                date=str(dates[i].strftime("%Y-%m-%d")),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
        )
        for i, b in enumerate(bars)
    ]


# ---------------------------------------------------------------------------
# Oscillating bar recipes
# ---------------------------------------------------------------------------


def _synthesise_oscillating_bars(pred: Predicate) -> Optional[List[OHLCVBar]]:
    """Route to the right recipe based on the predicate's operand types."""
    lhs = pred.lhs
    rhs = pred.rhs

    if _is_price_ref(lhs) and isinstance(rhs, (int, float)):
        return _oscillate_price_vs_number(lhs, pred.op, float(rhs))

    if isinstance(lhs, IndicatorRef) and isinstance(rhs, (int, float)):
        return _oscillate_indicator_vs_number(lhs, pred.op, float(rhs))

    if isinstance(lhs, IndicatorRef) and isinstance(rhs, IndicatorRef):
        return _oscillate_indicator_vs_indicator(lhs, rhs, pred.op)

    if _is_price_ref(lhs) and isinstance(rhs, IndicatorRef):
        return _oscillate_price_vs_indicator(lhs, rhs, pred.op)

    if isinstance(lhs, IndicatorRef) and _is_price_ref(rhs):
        return _oscillate_indicator_vs_price(lhs, rhs, pred.op)

    if _is_price_ref(lhs) and _is_price_ref(rhs):
        return _oscillate_price_vs_price(lhs, rhs, pred.op)

    return None


def _is_price_ref(side: Any) -> bool:
    return isinstance(side, str) and side.startswith("bar.")


def _price_field(ref: str) -> str:
    """Map ``"bar.close"`` → ``"close"`` etc."""
    return ref.split(".", 1)[1] if "." in ref else ref


def _make_bar(
    close: float,
    *,
    high_offset: float = 1.0,
    low_offset: float = 1.0,
    volume: float = _BASE_VOLUME,
) -> OHLCVBar:
    return OHLCVBar(
        date="placeholder",
        open=close,
        high=close + high_offset,
        low=max(0.01, close - low_offset),
        close=close,
        volume=volume,
    )


# --- price-ref vs number ---------------------------------------------------


def _oscillate_price_vs_number(
    lhs: str,
    op: str,
    threshold: float,
) -> List[OHLCVBar]:
    """Generate bars whose ``lhs`` field oscillates above/below ``threshold``.

    For cross ops, include 2-3 distinct crossing events.
    """
    margin = max(abs(threshold) * 0.1, 1.0)
    above = threshold + margin
    below = max(0.02, threshold - margin)
    fld = _price_field(lhs)

    if op in ("cross_above", "cross_below"):
        return _build_crossing_series(fld, below, above)

    if op == "==":
        bars: List[OHLCVBar] = []
        for cycle in range(6):
            val = threshold if cycle % 2 == 0 else above
            bars.extend(_segment(fld, val, _OSCILLATION_SEGMENT))
        return bars

    bars = []
    for cycle in range(6):
        val = below if cycle % 2 == 0 else above
        bars.extend(_segment(fld, val, _OSCILLATION_SEGMENT))
    return bars


# --- indicator vs number ---------------------------------------------------


def _oscillate_indicator_vs_number(
    lhs: IndicatorRef,
    op: str,
    threshold: float,
) -> Optional[List[OHLCVBar]]:
    """Build a price series whose indicator value crosses ``threshold``."""
    warmup = _warmup_for_indicator(lhs)
    total = max(_MIN_FIXTURE_BARS, warmup + 40)

    if lhs.name in ("sma", "ema"):
        return _oscillate_ma_vs_number(lhs, threshold, total, op)
    if lhs.name == "rsi":
        return _oscillate_rsi_vs_number(threshold, total, op, lhs)
    if lhs.name in (
        "macd",
        "bollinger",
        "atr",
        "adx",
        "stochastic",
        "vwap",
        "donchian",
        "keltner",
        "obv",
        "mfi",
        "cci",
        "williams_r",
    ):
        return _oscillate_generic_indicator(lhs, threshold, total, op)
    if lhs.name == "roc":
        # ROC is a momentum oscillator on a single source series, like the MAs —
        # a trending/oscillating price drives it through the threshold.
        return _oscillate_ma_vs_number(lhs, threshold, total, op)
    return None


def _oscillate_ma_vs_number(
    ref: IndicatorRef,
    threshold: float,
    total: int,
    op: str,
) -> List[OHLCVBar]:
    """For SMA/EMA vs number, oscillate price above/below threshold."""
    margin = max(abs(threshold) * 0.15, 2.0)
    above = threshold + margin
    below = max(0.02, threshold - margin)

    if op in ("cross_above", "cross_below"):
        return _build_trending_cross_series(below, above, total)

    bars: List[OHLCVBar] = []
    seg = max(total // 6, 8)
    for cycle in range(6):
        val = below if cycle % 2 == 0 else above
        bars.extend([_make_bar(val) for _ in range(seg)])
    return bars[:total]


def _oscillate_rsi_vs_number(
    threshold: float,
    total: int,
    op: str,
    ref: IndicatorRef,
) -> List[OHLCVBar]:
    """Build bars that drive RSI through the threshold.

    Declining prices push RSI below ``threshold``; recovering prices
    push it back above.
    """
    bars: List[OHLCVBar] = []
    close = _BASE_CLOSE
    seg = max(total // 4, 15)

    for cycle in range(4):
        if cycle % 2 == 0:
            step = -0.8
        else:
            step = 0.8
        for _ in range(seg):
            close = max(5.0, close + step)
            bars.append(_make_bar(close))

    return bars[:total]


def _oscillate_generic_indicator(
    ref: IndicatorRef,
    threshold: float,
    total: int,
    op: str,
) -> Optional[List[OHLCVBar]]:
    """Best-effort oscillating series for MACD, Bollinger, ATR, etc."""
    bars: List[OHLCVBar] = []
    close = _BASE_CLOSE
    seg = max(total // 4, 15)

    for cycle in range(4):
        if cycle % 2 == 0:
            step = -0.5
        else:
            step = 0.5
        for j in range(seg):
            close = max(5.0, close + step)
            vol_mult = 1.5 if cycle % 2 == 0 else 0.7
            bars.append(
                _make_bar(
                    close,
                    high_offset=abs(step) * 2,
                    low_offset=abs(step) * 2,
                    volume=_BASE_VOLUME * vol_mult,
                )
            )

    return bars[:total]


# --- indicator vs indicator ------------------------------------------------


def _oscillate_indicator_vs_indicator(
    lhs: IndicatorRef,
    rhs: IndicatorRef,
    op: str,
) -> Optional[List[OHLCVBar]]:
    """Build bars where two indicators cross each other."""
    warmup = max(_warmup_for_indicator(lhs), _warmup_for_indicator(rhs))
    total = max(_MIN_FIXTURE_BARS, warmup + 40)
    return _build_trending_cross_series(
        low_val=max(0.02, _BASE_CLOSE * 0.8),
        high_val=_BASE_CLOSE * 1.2,
        total=total,
    )


# --- price-ref vs indicator / indicator vs price-ref ----------------------


def _oscillate_price_vs_indicator(
    lhs: str,
    rhs: IndicatorRef,
    op: str,
) -> Optional[List[OHLCVBar]]:
    warmup = _warmup_for_indicator(rhs)
    total = max(_MIN_FIXTURE_BARS, warmup + 40)
    return _build_trending_cross_series(
        low_val=max(0.02, _BASE_CLOSE * 0.85),
        high_val=_BASE_CLOSE * 1.15,
        total=total,
    )


def _oscillate_indicator_vs_price(
    lhs: IndicatorRef,
    rhs: str,
    op: str,
) -> Optional[List[OHLCVBar]]:
    warmup = _warmup_for_indicator(lhs)
    total = max(_MIN_FIXTURE_BARS, warmup + 40)
    return _build_trending_cross_series(
        low_val=max(0.02, _BASE_CLOSE * 0.85),
        high_val=_BASE_CLOSE * 1.15,
        total=total,
    )


# --- price-ref vs price-ref -----------------------------------------------


def _oscillate_price_vs_price(
    lhs: str,
    rhs: str,
    op: str,
) -> Optional[List[OHLCVBar]]:
    """Vary the relationship between two price fields across bars.

    For ``close vs open``, ``high vs low``, etc.: alternate bars where
    lhs > rhs and bars where lhs < rhs so the predicate state changes.
    """
    lhs_f = _price_field(lhs)
    rhs_f = _price_field(rhs)
    bars: List[OHLCVBar] = []
    for cycle in range(6):
        for _ in range(_OSCILLATION_SEGMENT):
            if op == "==":
                if cycle % 2 == 0:
                    vals = {lhs_f: 100.0, rhs_f: 100.0}
                else:
                    vals = {lhs_f: 105.0, rhs_f: 95.0}
            elif cycle % 2 == 0:
                vals = {lhs_f: 105.0, rhs_f: 95.0}
            else:
                vals = {lhs_f: 95.0, rhs_f: 105.0}
            o = vals.get("open", 100.0)
            h = vals.get("high", 106.0)
            lo = vals.get("low", 94.0)
            c = vals.get("close", 100.0)
            v = vals.get("volume", _BASE_VOLUME)
            h = max(h, o, c, lo)
            lo = min(lo, o, c, h)
            bars.append(OHLCVBar(date="placeholder", open=o, high=h, low=lo, close=c, volume=v))
    return bars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _segment(field_name: str, value: float, n: int) -> List[OHLCVBar]:
    """Build ``n`` bars with the given field set to ``value``."""
    if field_name == "volume":
        return [_make_bar(_BASE_CLOSE, volume=value) for _ in range(n)]
    if field_name == "high":
        base = max(0.01, value - 1)
        return [
            OHLCVBar(
                date="placeholder",
                open=base,
                high=value,
                low=max(0.01, base - 1),
                close=base,
                volume=_BASE_VOLUME,
            )
            for _ in range(n)
        ]
    if field_name == "low":
        base = value + 1
        return [
            OHLCVBar(
                date="placeholder",
                open=base,
                high=base + 1,
                low=value,
                close=base,
                volume=_BASE_VOLUME,
            )
            for _ in range(n)
        ]
    return [_make_bar(value) for _ in range(n)]


def _build_crossing_series(field_name: str, low_val: float, high_val: float) -> List[OHLCVBar]:
    """Build bars with 2-3 crossing events for ``cross_above``/``cross_below``.

    Pattern: low → high → low → high → low (5 segments, 3 upward
    crossings and 2 downward crossings).
    """
    bars: List[OHLCVBar] = []
    seg = 12
    values = [low_val, high_val, low_val, high_val, low_val]

    def _bar_for_field(val: float) -> OHLCVBar:
        if field_name == "volume":
            return _make_bar(_BASE_CLOSE, volume=max(0.0, val))
        return _make_bar(max(0.02, val))

    for i, val in enumerate(values):
        if i > 0:
            prev_val = values[i - 1]
            for step in range(4):
                interp = prev_val + (val - prev_val) * (step + 1) / 4
                bars.append(_bar_for_field(interp))
        bars.extend([_bar_for_field(val) for _ in range(seg)])
    return bars


def _build_trending_cross_series(low_val: float, high_val: float, total: int) -> List[OHLCVBar]:
    """Build a V-shaped trending series that crosses its own indicators.

    Down-trending segment → flat → up-trending segment → flat.
    Creates regime changes that cause fast/slow MAs, MACD, etc. to cross.
    """
    seg = max(total // 4, 10)
    bars: List[OHLCVBar] = []

    for i in range(seg):
        close = high_val - (high_val - low_val) * i / seg
        bars.append(_make_bar(max(0.02, close)))
    for _ in range(seg):
        bars.append(_make_bar(low_val))
    for i in range(seg):
        close = low_val + (high_val - low_val) * i / seg
        bars.append(_make_bar(max(0.02, close)))
    for _ in range(seg):
        bars.append(_make_bar(high_val))

    return bars[:total]


def _warmup_for_indicator(ref: IndicatorRef) -> int:
    """Minimum bars before the indicator produces meaningful values."""
    if ref.name in ("sma", "ema", "rsi", "atr", "adx"):
        period = int(ref.param("period")) if "period" in ref.params else 14
        return period + 5
    if ref.name == "macd":
        slow = int(ref.param("slow")) if "slow" in ref.params else 26
        signal = int(ref.param("signal")) if "signal" in ref.params else 9
        return slow + signal + 5
    if ref.name == "bollinger":
        period = int(ref.param("period")) if "period" in ref.params else 20
        return period + 5
    if ref.name == "stochastic":
        k = int(ref.param("k_period")) if "k_period" in ref.params else 14
        d = int(ref.param("d_period")) if "d_period" in ref.params else 3
        return k + d + 5
    if ref.name in ("donchian", "cci", "williams_r", "mfi", "roc"):
        period = int(ref.param("period")) if "period" in ref.params else 14
        return period + 5
    if ref.name == "keltner":
        period = int(ref.param("period")) if "period" in ref.params else 20
        atr_period = int(ref.param("atr_period")) if "atr_period" in ref.params else 10
        return max(period, atr_period + 1) + 5
    return 20
