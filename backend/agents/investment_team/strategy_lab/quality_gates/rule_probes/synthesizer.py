"""Synthetic-bar recipes that force each spec rule's predicate to fire.

The compiler's ``on_bar`` is the contract the probes test against. Each
recipe produces a deterministic OHLCV sequence designed so that the
target rule's predicate evaluates ``True`` on a specific *trigger* bar,
verified up-front using the same indicator helpers the compiler emits
calls to (:mod:`investment_team.strategy_lab.executor.indicators`).

Recipes are organised by rule shape rather than rule kind:

- Entry rules dispatch on the ``Predicate`` shape (price-vs-number,
  indicator-vs-number, indicator-vs-indicator, cross-above/below).
- Exit rules reuse the entry recipes to build a position-opening prefix,
  then append a tail that satisfies the exit predicate (``StopLoss`` /
  ``TakeProfit`` / ``SignalExit``).

TimeStopRule is out of scope here — ``spec_dsl.py`` deliberately omits
it from the ``ExitRule`` union ("real traders close on price, P&L, or
signal reversal, not on an arbitrary Nth bar"). If the DSL grows a
time-based exit kind later, add a recipe here.

Unprobeable rules (e.g. predicates whose synthetic series cannot be
made to satisfy the predicate within bounded binary-search iterations,
or specs whose ``target_symbols`` mismatch the compiled ``UNIVERSE``
literal) return ``ProbeRun(synthesizable=False, unprobeable_reason=...)``
— the gate emits a warning rather than blocking the synthesis loop on a
limitation of this module.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional, Tuple

import pandas as pd

from ....market_data_service import OHLCVBar
from ...executor.indicators import (
    adx,
    atr,
    bollinger_bands,
    ema,
    macd,
    rsi,
    sma,
    stochastic,
    vwap,
)
from ...spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)

# Bars/recipe knobs. Indicators with the largest lookback need the most
# bars; ``min_total_bars`` keeps short-lookback recipes well above the
# compiler's ``history_depth`` so warm-up never starves them.
_MIN_TOTAL_BARS = 80
_BARS_AFTER_TRIGGER = 5
_DECAY_SEARCH_ITERS = 12
_PROBE_SYMBOL_FALLBACK = "PROBE"


@dataclass(frozen=True)
class ExpectedOutcome:
    """What the assertion layer expects to find in ``StrategyRunResult.trades``.

    Preconditions:
      - ``kind == "entry"`` → ``side`` is set to ``"long"`` / ``"short"``.
      - ``kind == "exit"`` → ``exit_reason_contains`` is set to a
        non-empty substring the engine writes into ``TradeRecord.exit_reason``
        for the rule kind under test (``"stop_loss"``, ``"take_profit"``,
        ``"signal_exit"``).
    """

    kind: Literal["entry", "exit"]
    side: Optional[Literal["long", "short"]] = None
    exit_reason_contains: Optional[str] = None


@dataclass(frozen=True)
class ProbeRun:
    """One probe's input + expected outcome.

    ``synthesizable=False`` runs skip the sandbox; the asserter emits a
    warning with ``unprobeable_reason``. ``trigger_bar_index`` is the
    index in ``market_data[symbol]`` of the bar where the predicate is
    expected to evaluate True (i.e. where a trade should appear at or
    after).
    """

    rule_id: str
    rule_kind: Literal["entry", "stop_loss", "take_profit", "signal_exit"]
    symbol: str
    market_data: List[OHLCVBar] = field(default_factory=list)
    expected: Optional[ExpectedOutcome] = None
    trigger_bar_index: int = 0
    synthesizable: bool = True
    unprobeable_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_rule_probe_runs(spec: Any, *, compiled_code: str = "") -> List[ProbeRun]:
    """Build one :class:`ProbeRun` per entry/exit rule in ``spec``.

    Pre:
      - ``spec`` is a ``StrategySpec`` carrying ``entry_rules`` and
        ``exit_rules`` lists.
      - ``compiled_code`` is optional. When supplied, the synthesiser
        parses any top-level ``UNIVERSE = frozenset({...})`` literal so
        the probe's synthetic symbol matches the compiled code's symbol
        filter (otherwise the sandbox's universe-guard returns at the
        top of ``on_bar`` and the probe sees zero trades).

    Post:
      - Returns exactly ``len(spec.entry_rules) + len(spec.exit_rules)``
        :class:`ProbeRun` objects, ordered entries-first.
      - Every probeable run's ``market_data`` carries unique, ascending
        ``date`` strings (sandbox callers require parseable dates).
      - Unprobeable rules carry ``synthesizable=False`` with a non-empty
        ``unprobeable_reason``; the asserter renders them as warnings.
    """
    symbol = _resolve_probe_symbol(spec, compiled_code)
    runs: List[ProbeRun] = []
    for idx, rule in enumerate(getattr(spec, "entry_rules", []) or []):
        runs.append(_build_entry_probe(rule, idx, symbol))
    for idx, rule in enumerate(getattr(spec, "exit_rules", []) or []):
        runs.append(_build_exit_probe(rule, idx, symbol, getattr(spec, "entry_rules", []) or []))
    return [_stamp_dates(run) for run in runs]


def _stamp_dates(probe: ProbeRun, start_date: str = "2024-01-01") -> ProbeRun:
    """Rebuild a probe's bars with ascending calendar dates and validate
    OHLC integrity.

    Recipe authors emit bars with placeholder ``date`` values for clarity —
    the actual dates are not meaningful, only their ordering. This pass
    assigns ``start_date + i days`` so downstream consumers (``BacktestConfig``,
    sandbox harness) get real, parseable date strings. It also clamps each
    bar's OHLC values so the downstream market-data preflight (which
    rejects nan_or_negative_prices and ohlc_violations) accepts the run.
    """
    if not probe.synthesizable or not probe.market_data:
        return probe
    dates = pd.date_range(start_date, periods=len(probe.market_data), freq="D")
    new_bars = [
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
        for i, b in enumerate(probe.market_data)
    ]
    return ProbeRun(
        rule_id=probe.rule_id,
        rule_kind=probe.rule_kind,
        symbol=probe.symbol,
        market_data=new_bars,
        expected=probe.expected,
        trigger_bar_index=probe.trigger_bar_index,
        synthesizable=probe.synthesizable,
        unprobeable_reason=probe.unprobeable_reason,
    )


# Floor for synthesised prices. The market-data preflight rejects any
# bar with an OHLC value <= 0 (``_has_nan_or_negative_price``), so we
# clamp here to keep recipes simple — none of the probe assertions
# care about absolute price levels, only relative motion.
_MIN_PRICE = 0.01


def _normalise_ohlc(bar: OHLCVBar) -> OHLCVBar:
    """Clamp OHLC values to satisfy the market-data preflight.

    Post:
      - every OHLC value is finite and > 0.
      - ``high >= max(open, close, low)``; ``low <= min(open, close, high)``.
      - ``volume`` is non-negative; NaN is replaced with 1.0.
    """

    def _safe(value: float) -> float:
        if value is None or not math.isfinite(value):
            return _MIN_PRICE
        return max(_MIN_PRICE, float(value))

    o = _safe(bar.open)
    c = _safe(bar.close)
    h = _safe(bar.high)
    low = _safe(bar.low)
    # Enforce the OHLC invariants the preflight checks.
    h = max(h, o, c, low)
    low = min(low, o, c, h)
    vol = bar.volume if bar.volume is not None and math.isfinite(bar.volume) and bar.volume >= 0 else 1.0
    return OHLCVBar(
        date=bar.date,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=vol,
    )


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def _resolve_probe_symbol(spec: Any, compiled_code: str) -> str:
    """Pick a synthetic-bar symbol that matches the compiled code's universe.

    Order of preference:
      1. ``spec.target_symbols[0]`` if non-empty.
      2. An element of the top-level ``UNIVERSE = frozenset({...})`` literal
         parsed out of ``compiled_code`` (if present and non-empty).
      3. The sentinel ``"PROBE"`` — only safe when the compiled code's
         universe filter is empty (i.e. no ``UNIVERSE`` reference in
         ``on_bar``); recipes that emit this sentinel and find a non-empty
         ``UNIVERSE`` literal mark themselves unprobeable downstream.
    """
    target_symbols = list(getattr(spec, "target_symbols", []) or [])
    if target_symbols:
        return str(target_symbols[0])
    parsed = _extract_universe_literal(compiled_code)
    if parsed:
        return next(iter(sorted(parsed)))
    return _PROBE_SYMBOL_FALLBACK


def _extract_universe_literal(code: str) -> frozenset:
    """Parse ``UNIVERSE = frozenset({...})`` (or assignment to ``self.UNIVERSE``)
    from compiled-strategy source. Returns an empty frozenset on any failure.
    """
    if not code:
        return frozenset()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return frozenset()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        target_name = None
        if isinstance(target, ast.Name):
            target_name = target.id
        elif isinstance(target, ast.Attribute):
            target_name = target.attr
        if target_name != "UNIVERSE":
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
            if not value.args:
                return frozenset()
            arg = value.args[0]
            try:
                literal = ast.literal_eval(arg)
            except (ValueError, SyntaxError):
                return frozenset()
            if isinstance(literal, (set, frozenset, list, tuple)):
                return frozenset(str(s) for s in literal)
    return frozenset()


# ---------------------------------------------------------------------------
# Entry probes
# ---------------------------------------------------------------------------


def _build_entry_probe(rule: EntryRule, idx: int, symbol: str) -> ProbeRun:
    rule_id = f"entry[{idx}]"
    bars, trigger_idx, reason = _synthesise_for_predicate(rule.when)
    if bars is None:
        return ProbeRun(
            rule_id=rule_id,
            rule_kind="entry",
            symbol=symbol,
            synthesizable=False,
            unprobeable_reason=reason or "predicate_not_synthesizable",
        )
    return ProbeRun(
        rule_id=rule_id,
        rule_kind="entry",
        symbol=symbol,
        market_data=bars,
        expected=ExpectedOutcome(kind="entry", side=rule.side),
        trigger_bar_index=trigger_idx,
    )


# ---------------------------------------------------------------------------
# Exit probes — entry-prefix + exit-tail composition
# ---------------------------------------------------------------------------


def _build_exit_probe(
    rule: Any,
    idx: int,
    symbol: str,
    entry_rules: List[EntryRule],
) -> ProbeRun:
    kind = getattr(rule, "kind", None)
    rule_id = f"exit[{idx}]:{kind}"
    if not entry_rules:
        return ProbeRun(
            rule_id=rule_id,
            rule_kind=kind or "signal_exit",
            symbol=symbol,
            synthesizable=False,
            unprobeable_reason="no_entry_rules_to_open_position",
        )
    # Reuse entry[0]'s recipe to open the position, then append an exit
    # tail. The opening trigger bar becomes our entry reference for the
    # exit recipe.
    entry_bars, entry_trigger_idx, entry_reason = _synthesise_for_predicate(entry_rules[0].when)
    if entry_bars is None:
        return ProbeRun(
            rule_id=rule_id,
            rule_kind=kind or "signal_exit",
            symbol=symbol,
            synthesizable=False,
            unprobeable_reason=f"entry_prefix_not_synthesizable: {entry_reason}",
        )
    entry_close = entry_bars[entry_trigger_idx].close
    entry_side = entry_rules[0].side

    if isinstance(rule, StopLossRule):
        tail, tail_trigger_offset = _synthesise_stop_loss_tail(rule, entry_close, entry_side)
        if tail is None:
            return ProbeRun(
                rule_id=rule_id,
                rule_kind="stop_loss",
                symbol=symbol,
                synthesizable=False,
                unprobeable_reason="stop_loss_tail_not_synthesizable",
            )
        full_bars = _stitch(entry_bars, tail)
        return ProbeRun(
            rule_id=rule_id,
            rule_kind="stop_loss",
            symbol=symbol,
            market_data=full_bars,
            expected=ExpectedOutcome(kind="exit", exit_reason_contains="stop_loss"),
            trigger_bar_index=len(entry_bars) + tail_trigger_offset,
        )
    if isinstance(rule, TakeProfitRule):
        tail, tail_trigger_offset = _synthesise_take_profit_tail(rule, entry_close, entry_side)
        if tail is None:
            return ProbeRun(
                rule_id=rule_id,
                rule_kind="take_profit",
                symbol=symbol,
                synthesizable=False,
                unprobeable_reason="take_profit_tail_not_synthesizable",
            )
        full_bars = _stitch(entry_bars, tail)
        return ProbeRun(
            rule_id=rule_id,
            rule_kind="take_profit",
            symbol=symbol,
            market_data=full_bars,
            expected=ExpectedOutcome(kind="exit", exit_reason_contains="take_profit"),
            trigger_bar_index=len(entry_bars) + tail_trigger_offset,
        )
    if isinstance(rule, SignalExitRule):
        tail_bars, tail_trigger_idx, tail_reason = _synthesise_for_predicate(
            rule.when,
            base_close=entry_close,
            min_bars=20,
        )
        if tail_bars is None:
            return ProbeRun(
                rule_id=rule_id,
                rule_kind="signal_exit",
                symbol=symbol,
                synthesizable=False,
                unprobeable_reason=f"signal_exit_tail_not_synthesizable: {tail_reason}",
            )
        full_bars = _stitch(entry_bars, tail_bars)
        return ProbeRun(
            rule_id=rule_id,
            rule_kind="signal_exit",
            symbol=symbol,
            market_data=full_bars,
            # Signal exits in compiler-generated code emit
            # ``reason="compiled_signal_exit"``; substring "signal_exit"
            # catches both that and any future "engine_exit:signal_exit"
            # prefix the engine might adopt later.
            expected=ExpectedOutcome(kind="exit", exit_reason_contains="signal_exit"),
            trigger_bar_index=len(entry_bars) + tail_trigger_idx,
        )
    return ProbeRun(
        rule_id=rule_id,
        rule_kind="signal_exit",
        symbol=symbol,
        synthesizable=False,
        unprobeable_reason=f"unknown_exit_rule_type:{type(rule).__name__}",
    )


def _stitch(prefix: List[OHLCVBar], suffix: List[OHLCVBar]) -> List[OHLCVBar]:
    """Concatenate two bar lists. Dates are re-stamped at the top level
    by :func:`_stamp_dates` so this helper does not touch ``date``."""
    return list(prefix) + list(suffix)


# ---------------------------------------------------------------------------
# Stop-loss / take-profit tails
# ---------------------------------------------------------------------------


def _synthesise_stop_loss_tail(
    rule: StopLossRule, entry_close: float, entry_side: str
) -> Tuple[Optional[List[OHLCVBar]], int]:
    """Return a few quiet bars followed by one adversarial bar that pierces
    the stop. ``basis="trailing_*"`` reuses the entry_price floor as a
    conservative approximation — the rule still fires because price moves
    far enough.
    """
    if rule.basis == "trailing_low" and entry_side == "long":
        return None, 0  # Engine treats this as a no-op for longs.
    if rule.basis == "trailing_high" and entry_side == "short":
        return None, 0
    epsilon = 0.005
    if entry_side == "long":
        # Long stop fires when bar.low <= entry_price * (1 - pct).
        adversarial_close = entry_close * (1.0 - rule.pct - epsilon)
        adversarial_low = adversarial_close - 0.01
        adversarial_high = entry_close * (1.0 - rule.pct / 2.0)
    else:
        # Short stop fires when bar.high >= entry_price * (1 + pct).
        adversarial_close = entry_close * (1.0 + rule.pct + epsilon)
        adversarial_high = adversarial_close + 0.01
        adversarial_low = entry_close * (1.0 + rule.pct / 2.0)
    quiet_bars = [
        OHLCVBar(
            date="placeholder",
            open=entry_close,
            high=entry_close + 0.01,
            low=entry_close - 0.01,
            close=entry_close,
            volume=1_000_000.0,
        )
        for _ in range(3)
    ]
    trigger_bar = OHLCVBar(
        date="placeholder",
        open=entry_close,
        high=adversarial_high,
        low=adversarial_low,
        close=adversarial_close,
        volume=1_000_000.0,
    )
    trail = [
        OHLCVBar(
            date="placeholder",
            open=adversarial_close,
            high=adversarial_close + 0.01,
            low=adversarial_close - 0.01,
            close=adversarial_close,
            volume=1_000_000.0,
        )
        for _ in range(_BARS_AFTER_TRIGGER)
    ]
    return quiet_bars + [trigger_bar] + trail, len(quiet_bars)


def _synthesise_take_profit_tail(
    rule: TakeProfitRule, entry_close: float, entry_side: str
) -> Tuple[Optional[List[OHLCVBar]], int]:
    epsilon = 0.005
    if entry_side == "long":
        adversarial_close = entry_close * (1.0 + rule.pct + epsilon)
        adversarial_high = adversarial_close + 0.01
        adversarial_low = entry_close * (1.0 + rule.pct / 2.0)
    else:
        adversarial_close = entry_close * (1.0 - rule.pct - epsilon)
        adversarial_low = adversarial_close - 0.01
        adversarial_high = entry_close * (1.0 - rule.pct / 2.0)
    quiet_bars = [
        OHLCVBar(
            date="placeholder",
            open=entry_close,
            high=entry_close + 0.01,
            low=entry_close - 0.01,
            close=entry_close,
            volume=1_000_000.0,
        )
        for _ in range(3)
    ]
    trigger_bar = OHLCVBar(
        date="placeholder",
        open=entry_close,
        high=adversarial_high,
        low=adversarial_low,
        close=adversarial_close,
        volume=1_000_000.0,
    )
    trail = [
        OHLCVBar(
            date="placeholder",
            open=adversarial_close,
            high=adversarial_close + 0.01,
            low=adversarial_close - 0.01,
            close=adversarial_close,
            volume=1_000_000.0,
        )
        for _ in range(_BARS_AFTER_TRIGGER)
    ]
    return quiet_bars + [trigger_bar] + trail, len(quiet_bars)


# ---------------------------------------------------------------------------
# Entry predicate dispatch
# ---------------------------------------------------------------------------


def _synthesise_for_predicate(
    pred: Predicate,
    *,
    base_close: float = 100.0,
    min_bars: int = _MIN_TOTAL_BARS,
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Dispatch on the predicate's lhs/rhs/op shape.

    Returns ``(bars, trigger_index, unprobeable_reason)``. ``bars=None``
    signals "cannot synthesise"; ``unprobeable_reason`` is the diagnostic
    the gate surfaces to the operator.
    """
    lhs, op, rhs = pred.lhs, pred.op, pred.rhs

    # Cross ops route through the cross dispatcher regardless of side shapes —
    # the (prev, curr)-pair semantics of cross-above/below differ from a
    # plain inequality, so the recipe must be the cross-specific one.
    if op in ("cross_above", "cross_below"):
        return _synth_cross(lhs, op, rhs, base_close, min_bars)

    # Trivial: PriceRef vs float / PriceRef.
    if isinstance(lhs, str) and isinstance(rhs, (int, float)) and not isinstance(rhs, bool):
        return _synth_priceref_vs_number(lhs, op, float(rhs), base_close, min_bars)
    if isinstance(lhs, str) and isinstance(rhs, str):
        return _synth_priceref_vs_priceref(lhs, op, rhs, base_close, min_bars)

    # Indicator on the lhs against a number.
    if isinstance(lhs, IndicatorRef) and isinstance(rhs, (int, float)) and not isinstance(rhs, bool):
        return _synth_indicator_vs_number(lhs, op, float(rhs), base_close, min_bars)

    # Indicator vs Indicator (e.g. SMA(10) > SMA(50)).
    if isinstance(lhs, IndicatorRef) and isinstance(rhs, IndicatorRef):
        return _synth_indicator_vs_indicator(lhs, op, rhs, base_close, min_bars)

    # Indicator vs PriceRef (e.g. SMA(50) > bar.close).
    if isinstance(lhs, IndicatorRef) and isinstance(rhs, str):
        return _synth_indicator_vs_priceref(lhs, op, rhs, base_close, min_bars)

    return None, 0, f"unsupported_predicate_shape:{type(lhs).__name__}_{op}_{type(rhs).__name__}"


# ---------------------------------------------------------------------------
# Recipe: PriceRef vs number
# ---------------------------------------------------------------------------


def _synth_priceref_vs_number(
    lhs: str, op: str, rhs: float, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Generate bars where the bar's price field satisfies ``lhs op rhs``."""
    n = max(min_bars, 30)
    trigger_idx = n - _BARS_AFTER_TRIGGER - 1
    bars: List[OHLCVBar] = []
    field_name = lhs.split(".", 1)[1]  # "bar.close" -> "close"
    # Baseline closes that don't satisfy the predicate.
    baseline = rhs - 5.0 if op in (">", ">=") else rhs + 5.0
    for i in range(n):
        if i == trigger_idx:
            # Move the relevant field across the threshold.
            if op == ">":
                value = rhs + 1.0
            elif op == ">=":
                value = rhs + 0.5
            elif op == "<":
                value = rhs - 1.0
            elif op == "<=":
                value = rhs - 0.5
            elif op == "==":
                value = rhs
            else:
                return None, 0, f"unsupported_priceref_op:{op}"
            bars.append(_bar_with_field(field_name, value))
        else:
            bars.append(_bar_with_field(field_name, baseline))
    if not _verify_priceref_vs_number(bars, field_name, op, rhs, trigger_idx):
        return None, 0, "priceref_vs_number_verification_failed"
    return bars, trigger_idx, None


def _bar_with_field(field_name: str, value: float) -> OHLCVBar:
    """Build a bar where ``field_name`` is set to ``value`` and the other
    OHLC fields are consistent (high >= max(open, close), low <= min(...))."""
    open_ = close = high = low = value
    if field_name == "close":
        open_ = value
        high = value + 0.5
        low = value - 0.5
    elif field_name == "high":
        close = value - 0.5
        open_ = close
        low = close - 0.5
    elif field_name == "low":
        close = value + 0.5
        open_ = close
        high = close + 0.5
    elif field_name == "volume":
        open_ = close = high = low = 100.0
        return OHLCVBar(date="placeholder", open=100.0, high=100.5, low=99.5, close=100.0, volume=value)
    return OHLCVBar(
        date="placeholder",
        open=open_,
        high=max(high, open_, close),
        low=min(low, open_, close),
        close=close,
        volume=1_000_000.0,
    )


def _verify_priceref_vs_number(
    bars: List[OHLCVBar], field_name: str, op: str, rhs: float, idx: int
) -> bool:
    bar = bars[idx]
    value = getattr(bar, field_name)
    return _compare(value, op, rhs)


# ---------------------------------------------------------------------------
# Recipe: PriceRef vs PriceRef (e.g. bar.close > bar.open)
# ---------------------------------------------------------------------------


def _synth_priceref_vs_priceref(
    lhs: str, op: str, rhs: str, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    if lhs == rhs:
        return None, 0, "priceref_vs_self_unsatisfiable"
    n = max(min_bars, 30)
    trigger_idx = n - _BARS_AFTER_TRIGGER - 1
    bars: List[OHLCVBar] = []
    lhs_field = lhs.split(".", 1)[1]
    rhs_field = rhs.split(".", 1)[1]
    # Baseline bar where lhs == rhs (predicate inert).
    for i in range(n):
        if i == trigger_idx:
            bars.append(_bar_with_priceref_relation(lhs_field, rhs_field, op))
        else:
            bars.append(
                OHLCVBar(
                    date="placeholder",
                    open=base_close,
                    high=base_close + 1.0,
                    low=base_close - 1.0,
                    close=base_close,
                    volume=1_000_000.0,
                )
            )
    return bars, trigger_idx, None


def _bar_with_priceref_relation(lhs_field: str, rhs_field: str, op: str) -> OHLCVBar:
    """Build a bar where the bar's ``lhs_field`` and ``rhs_field`` satisfy ``op``."""
    # Default OHLC: open=100, low=99, high=101, close=100.5.
    open_ = 100.0
    low = 99.0
    high = 101.0
    close = 100.5
    fields = {"open": open_, "low": low, "high": high, "close": close}
    lhs_val = fields[lhs_field]
    rhs_val = fields[rhs_field]
    if _compare(lhs_val, op, rhs_val):
        return OHLCVBar(
            date="placeholder",
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=1_000_000.0,
        )
    # Adjust the lhs field to satisfy the predicate.
    if op in (">", ">="):
        target = rhs_val + 1.0
    elif op in ("<", "<="):
        target = rhs_val - 1.0
    else:
        target = rhs_val
    fields[lhs_field] = target
    new_high = max(fields["open"], fields["close"], fields["high"], target)
    new_low = min(fields["open"], fields["close"], fields["low"], target)
    return OHLCVBar(
        date="placeholder",
        open=fields["open"],
        high=new_high,
        low=new_low,
        close=fields["close"],
        volume=1_000_000.0,
    )


# ---------------------------------------------------------------------------
# Recipe: Indicator vs number
# ---------------------------------------------------------------------------


def _synth_indicator_vs_number(
    ref: IndicatorRef, op: str, rhs: float, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Drive ``indicator(closes) op rhs`` at the trigger bar by shaping the closes.

    Strategy per indicator:
      - ``rsi``: geometric decline (op in <, <=) or incline (>, >=); binary-search the rate.
      - ``sma`` / ``ema``: drive the moving average above/below ``rhs`` by setting closes accordingly.
      - ``macd`` (output=macd): trending series; pre-flight verification picks the right slope.
      - ``bollinger``: combination of high-vol then breakout; verification confirms.
      - ``atr`` / ``adx`` / ``stochastic`` / ``vwap``: synth series, then verify; bail if the
        requested threshold isn't reachable.
    """
    n = max(min_bars, _required_bars_for_indicator(ref))
    trigger_idx = n - _BARS_AFTER_TRIGGER - 1
    if ref.name in ("sma", "ema"):
        # SMA/EMA flat-line at ``rhs`` ± delta hits the predicate trivially.
        if op in (">", ">="):
            level = rhs + 1.0
        elif op in ("<", "<="):
            level = rhs - 1.0
        else:
            level = rhs
        bars = _flat_bars(level, n)
    elif ref.name == "rsi":
        bars = _rsi_search_bars(ref, op, rhs, n, base_close, trigger_idx)
        if bars is None:
            return None, 0, "rsi_threshold_unreachable"
    elif ref.name == "macd":
        bars = _macd_bars(ref, op, rhs, n, base_close, trigger_idx)
        if bars is None:
            return None, 0, "macd_threshold_unreachable"
    elif ref.name == "bollinger":
        bars = _bollinger_bars(ref, op, rhs, n, base_close, trigger_idx)
        if bars is None:
            return None, 0, "bollinger_threshold_unreachable"
    elif ref.name in ("atr", "adx", "stochastic", "vwap"):
        bars = _high_volatility_bars(n, base_close, trigger_idx)
    else:
        return None, 0, f"unsupported_indicator:{ref.name}"

    if not _verify_indicator_vs_number(bars, ref, op, rhs, trigger_idx):
        return None, 0, f"indicator_predicate_verification_failed:{ref.name}_{op}"
    return bars, trigger_idx, None


def _flat_bars(close: float, n: int) -> List[OHLCVBar]:
    return [
        OHLCVBar(
            date="placeholder",
            open=close,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=1_000_000.0,
        )
        for _ in range(n)
    ]


def _rsi_search_bars(
    ref: IndicatorRef, op: str, rhs: float, n: int, base_close: float, trigger_idx: int
) -> Optional[List[OHLCVBar]]:
    """Binary-search a per-step return so RSI at ``trigger_idx`` satisfies the predicate.

    For ``rsi < t`` we want sustained losses (negative returns); for ``rsi > t`` sustained gains.
    """
    if op in ("<", "<="):
        # Negative returns drive RSI down.
        lo, hi = 0.001, 0.05
        target_below = True
    elif op in (">", ">="):
        lo, hi = 0.001, 0.05
        target_below = False
    else:
        return None

    def closes_for(step: float) -> List[float]:
        if target_below:
            return [base_close * ((1.0 - step) ** i) for i in range(n)]
        return [base_close * ((1.0 + step) ** i) for i in range(n)]

    def rsi_at_trigger(step: float) -> float:
        series = pd.Series(closes_for(step))
        period = int(ref.param("period"))
        value = rsi(series, period=period).iloc[trigger_idx]
        if isinstance(value, float) and not math.isfinite(value):
            return 100.0 if not target_below else 0.0
        return float(value)

    for _ in range(_DECAY_SEARCH_ITERS):
        mid = (lo + hi) / 2.0
        v = rsi_at_trigger(mid)
        if _compare(v, op, rhs):
            closes = closes_for(mid)
            return [
                OHLCVBar(
                    date="placeholder",
                    open=c,
                    high=c + 0.01,
                    low=c - 0.01,
                    close=c,
                    volume=1_000_000.0,
                )
                for c in closes
            ]
        if target_below:
            lo = mid  # Need stronger decline.
        else:
            hi = mid  # Already above; try gentler step.
    # Last-chance: use the steepest step and check anyway.
    closes = closes_for(hi if target_below else lo)
    series = pd.Series(closes)
    if _compare(rsi(series, period=int(ref.param("period"))).iloc[trigger_idx], op, rhs):
        return [
            OHLCVBar(
                date="placeholder",
                open=c,
                high=c + 0.01,
                low=c - 0.01,
                close=c,
                volume=1_000_000.0,
            )
            for c in closes
        ]
    return None


def _macd_bars(
    ref: IndicatorRef, op: str, rhs: float, n: int, base_close: float, trigger_idx: int
) -> Optional[List[OHLCVBar]]:
    """A simple monotonically-trending series produces a non-zero MACD."""
    slope = 0.5 if op in (">", ">=") else -0.5
    closes = [base_close + slope * i for i in range(n)]
    closes = [max(c, 1.0) for c in closes]  # Prevent zero/negative prices.
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.01,
            low=c - 0.01,
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _bollinger_bars(
    ref: IndicatorRef, op: str, rhs: float, n: int, base_close: float, trigger_idx: int
) -> Optional[List[OHLCVBar]]:
    """Quiet history then a breakout bar — moves the requested band relative to ``rhs``."""
    closes = [base_close] * (n - 5) + [base_close + 10.0 * i for i in range(1, 6)]
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _high_volatility_bars(n: int, base_close: float, trigger_idx: int) -> List[OHLCVBar]:
    """Alternating up/down bars produce non-trivial ATR/ADX/Stochastic/VWAP values."""
    bars: List[OHLCVBar] = []
    for i in range(n):
        sign = 1 if i % 2 == 0 else -1
        close = base_close + sign * 3.0 + i * 0.1
        bars.append(
            OHLCVBar(
                date="placeholder",
                open=close,
                high=close + 2.0,
                low=close - 2.0,
                close=close,
                volume=1_000_000.0,
            )
        )
    return bars


def _verify_indicator_vs_number(
    bars: List[OHLCVBar], ref: IndicatorRef, op: str, rhs: float, idx: int
) -> bool:
    value = _compute_indicator_at(ref, bars, idx)
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return False
    return _compare(value, op, rhs)


def _compute_indicator_at(ref: IndicatorRef, bars: List[OHLCVBar], idx: int) -> Optional[float]:
    """Compute ``ref`` over the synthetic bar series and return the value at ``idx``."""
    df = _bars_to_df(bars)
    series = _series_for_source(df, ref.source)
    if ref.name == "sma":
        v = sma(series, int(ref.param("period"))).iloc[idx]
    elif ref.name == "ema":
        v = ema(series, int(ref.param("period"))).iloc[idx]
    elif ref.name == "rsi":
        v = rsi(series, int(ref.param("period"))).iloc[idx]
    elif ref.name == "macd":
        line, signal_, hist = macd(
            series,
            int(ref.param("fast")),
            int(ref.param("slow")),
            int(ref.param("signal")),
        )
        output = ref.param("output")
        v = {"macd": line, "signal": signal_, "histogram": hist}[output].iloc[idx]
    elif ref.name == "bollinger":
        upper, middle, lower = bollinger_bands(
            series, int(ref.param("period")), float(ref.param("num_std"))
        )
        band = ref.param("band")
        v = {"upper": upper, "middle": middle, "lower": lower}[band].iloc[idx]
    elif ref.name == "atr":
        v = atr(df["high"], df["low"], df["close"], int(ref.param("period"))).iloc[idx]
    elif ref.name == "adx":
        v = adx(df["high"], df["low"], df["close"], int(ref.param("period"))).iloc[idx]
    elif ref.name == "stochastic":
        k, d = stochastic(
            df["high"], df["low"], df["close"], int(ref.param("k_period")), int(ref.param("d_period"))
        )
        output = ref.param("output")
        v = {"k": k, "d": d}[output].iloc[idx]
    elif ref.name == "vwap":
        v = vwap(df["high"], df["low"], df["close"], df["volume"]).iloc[idx]
    else:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return float(v)


def _series_for_source(df: pd.DataFrame, source: str) -> pd.Series:
    if source == "close":
        return df["close"]
    if source == "open":
        return df["open"]
    if source == "high":
        return df["high"]
    if source == "low":
        return df["low"]
    if source == "volume":
        return df["volume"]
    if source == "hl2":
        return (df["high"] + df["low"]) / 2.0
    if source == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    return df["close"]


def _bars_to_df(bars: List[OHLCVBar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )


def _required_bars_for_indicator(ref: IndicatorRef) -> int:
    if ref.name in ("sma", "ema", "rsi", "atr", "adx"):
        period = int(ref.param("period")) if "period" in ref.params or ref.name in ("rsi", "atr", "adx") else 20
        return max(_MIN_TOTAL_BARS, period + 30)
    if ref.name == "macd":
        slow = int(ref.param("slow"))
        signal_ = int(ref.param("signal"))
        return max(_MIN_TOTAL_BARS, slow + signal_ + 30)
    if ref.name == "bollinger":
        period = int(ref.param("period"))
        return max(_MIN_TOTAL_BARS, period + 30)
    if ref.name == "stochastic":
        k = int(ref.param("k_period"))
        d = int(ref.param("d_period"))
        return max(_MIN_TOTAL_BARS, k + d + 30)
    return _MIN_TOTAL_BARS


# ---------------------------------------------------------------------------
# Recipe: cross_above / cross_below
# ---------------------------------------------------------------------------


def _synth_cross(
    lhs: Any, op: str, rhs: Any, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """``cross_above(lhs, rhs)`` requires prev: lhs <= rhs, curr: lhs > rhs."""
    # PriceRef cross IndicatorRef — the common case (close cross_above SMA(50)).
    if isinstance(lhs, str) and isinstance(rhs, IndicatorRef):
        return _synth_cross_priceref_indicator(lhs, op, rhs, base_close, min_bars)
    # IndicatorRef cross IndicatorRef (e.g. fast SMA cross slow SMA).
    if isinstance(lhs, IndicatorRef) and isinstance(rhs, IndicatorRef):
        return _synth_cross_indicator_indicator(lhs, op, rhs, base_close, min_bars)
    # IndicatorRef cross PriceRef (rare; symmetric to above).
    if isinstance(lhs, IndicatorRef) and isinstance(rhs, str):
        return _synth_cross_indicator_priceref(lhs, op, rhs, base_close, min_bars)
    # IndicatorRef cross float.
    if isinstance(lhs, IndicatorRef) and isinstance(rhs, (int, float)) and not isinstance(rhs, bool):
        return _synth_cross_indicator_number(lhs, op, float(rhs), base_close, min_bars)
    return None, 0, f"unsupported_cross_shape:{type(lhs).__name__}_{type(rhs).__name__}"


def _synth_cross_priceref_indicator(
    lhs: str, op: str, rhs: IndicatorRef, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Long flat history at ``base_close`` (so MA == base_close), then a single
    bar with close above/below that level."""
    if rhs.name not in ("sma", "ema"):
        return None, 0, f"cross_against_unsupported_indicator:{rhs.name}"
    if lhs != "bar.close":
        return None, 0, f"cross_against_unsupported_priceref:{lhs}"
    n = max(min_bars, int(rhs.param("period")) + 30)
    trigger_idx = n - _BARS_AFTER_TRIGGER - 1
    bars = _flat_bars(base_close, n)
    delta = 5.0 if op == "cross_above" else -5.0
    triggered_close = base_close + delta
    bars[trigger_idx] = OHLCVBar(
        date="placeholder",
        open=base_close,
        high=max(base_close, triggered_close) + 0.5,
        low=min(base_close, triggered_close) - 0.5,
        close=triggered_close,
        volume=1_000_000.0,
    )
    # Verify the cross actually evaluates true with the (prev, curr) pair.
    df = _bars_to_df(bars)
    ma_series = (sma if rhs.name == "sma" else ema)(df["close"], int(rhs.param("period")))
    prev_close = df["close"].iloc[trigger_idx - 1]
    cur_close = df["close"].iloc[trigger_idx]
    prev_ma = ma_series.iloc[trigger_idx - 1]
    cur_ma = ma_series.iloc[trigger_idx]
    if not _verify_cross(prev_close, cur_close, prev_ma, cur_ma, op):
        return None, 0, "cross_priceref_indicator_verification_failed"
    return bars, trigger_idx, None


def _synth_cross_indicator_indicator(
    lhs: IndicatorRef, op: str, rhs: IndicatorRef, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Two SMAs / EMAs cross when the underlying series changes regime."""
    if lhs.name not in ("sma", "ema") or rhs.name not in ("sma", "ema"):
        return None, 0, f"indicator_cross_unsupported:{lhs.name}_{rhs.name}"
    lhs_period = int(lhs.param("period"))
    rhs_period = int(rhs.param("period"))
    longest = max(lhs_period, rhs_period)
    n = max(min_bars, longest * 2 + 30)
    # Regime 1: declining; regime 2: rising. Fast MA crosses slow MA at the
    # regime change.
    midpoint = n // 2
    closes: List[float] = []
    for i in range(n):
        if i < midpoint:
            closes.append(base_close * (0.98 ** (i)))
        else:
            closes.append(closes[-1] * 1.03)
    closes = [max(c, 1.0) for c in closes]
    bars = [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]
    df = _bars_to_df(bars)
    lhs_series = (sma if lhs.name == "sma" else ema)(df["close"], lhs_period)
    rhs_series = (sma if rhs.name == "sma" else ema)(df["close"], rhs_period)
    # Scan for a bar where the cross occurs.
    for idx in range(longest + 1, n):
        if _verify_cross(
            lhs_series.iloc[idx - 1],
            lhs_series.iloc[idx],
            rhs_series.iloc[idx - 1],
            rhs_series.iloc[idx],
            op,
        ):
            return bars, idx, None
    return None, 0, "indicator_indicator_cross_not_found_in_window"


def _synth_cross_indicator_priceref(
    lhs: IndicatorRef, op: str, rhs: str, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    # Symmetric to priceref-vs-indicator with sides swapped — flip the op.
    flipped = "cross_above" if op == "cross_below" else "cross_below"
    return _synth_cross_priceref_indicator(rhs, flipped, lhs, base_close, min_bars)


def _synth_cross_indicator_number(
    lhs: IndicatorRef, op: str, rhs: float, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Build a series where the indicator crosses the threshold ``rhs``.

    Strategy: flat regime far below (or above) ``rhs`` long enough for the
    indicator to settle, then a sustained burst at a wider distance so
    the moving average pulls past ``rhs`` over several bars.
    """
    if lhs.name in ("sma", "ema"):
        period = int(lhs.param("period"))
        n = max(min_bars, period * 3 + 30)
        # Quiet regime + spike regime; spike magnitude needs to be large
        # enough that the moving average over the second half clears rhs.
        if op == "cross_above":
            below_level = rhs - 10.0
            spike_level = rhs + max(20.0, rhs)
            closes = [below_level] * (n // 2) + [spike_level] * (n - n // 2)
        else:
            above_level = rhs + 10.0
            crash_level = max(_MIN_PRICE, rhs - max(20.0, rhs))
            closes = [above_level] * (n // 2) + [crash_level] * (n - n // 2)
        bars = [
            OHLCVBar(
                date="placeholder",
                open=c,
                high=c + 0.5,
                low=c - 0.5,
                close=c,
                volume=1_000_000.0,
            )
            for c in closes
        ]
        df = _bars_to_df(bars)
        ma_series = (sma if lhs.name == "sma" else ema)(df["close"], int(lhs.param("period")))
        for idx in range(int(lhs.param("period")) + 1, n):
            if _verify_cross(
                ma_series.iloc[idx - 1], ma_series.iloc[idx], rhs, rhs, op
            ):
                return bars, idx, None
        return None, 0, "indicator_number_cross_not_found"
    return None, 0, f"cross_indicator_number_unsupported:{lhs.name}"


def _verify_cross(prev_l: Any, cur_l: Any, prev_r: Any, cur_r: Any, op: str) -> bool:
    """Mirror the (prev, curr)-pair semantics the compiled code uses."""
    try:
        prev_l_f = float(prev_l)
        cur_l_f = float(cur_l)
        prev_r_f = float(prev_r)
        cur_r_f = float(cur_r)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) for x in (prev_l_f, cur_l_f, prev_r_f, cur_r_f)):
        return False
    if op == "cross_above":
        return prev_l_f <= prev_r_f and cur_l_f > cur_r_f
    if op == "cross_below":
        return prev_l_f >= prev_r_f and cur_l_f < cur_r_f
    return False


# ---------------------------------------------------------------------------
# Recipe: Indicator vs Indicator (non-cross)
# ---------------------------------------------------------------------------


def _synth_indicator_vs_indicator(
    lhs: IndicatorRef, op: str, rhs: IndicatorRef, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """For non-cross comparisons: build a series where both indicators are
    computable and the inequality holds at the trigger bar."""
    if {lhs.name, rhs.name} - {"sma", "ema"}:
        return None, 0, f"indicator_vs_indicator_unsupported:{lhs.name}_{rhs.name}"
    lhs_period = int(lhs.param("period"))
    rhs_period = int(rhs.param("period"))
    longest = max(lhs_period, rhs_period)
    n = max(min_bars, longest + 30)
    # Trending series produces lhs-fast > rhs-slow at the right edge.
    closes = [base_close + i * 0.5 for i in range(n)]
    if op in ("<", "<="):
        closes = list(reversed(closes))
    bars = [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]
    df = _bars_to_df(bars)
    lhs_series = (sma if lhs.name == "sma" else ema)(df["close"], lhs_period)
    rhs_series = (sma if rhs.name == "sma" else ema)(df["close"], rhs_period)
    for idx in range(longest, n):
        lv = lhs_series.iloc[idx]
        rv = rhs_series.iloc[idx]
        if isinstance(lv, float) and not math.isfinite(lv):
            continue
        if isinstance(rv, float) and not math.isfinite(rv):
            continue
        if _compare(float(lv), op, float(rv)):
            return bars, idx, None
    return None, 0, "indicator_vs_indicator_no_satisfying_bar"


def _synth_indicator_vs_priceref(
    lhs: IndicatorRef, op: str, rhs: str, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Indicator-vs-PriceRef: drive the indicator value relative to the bar's price field."""
    n = max(min_bars, _required_bars_for_indicator(lhs))
    if lhs.name in ("sma", "ema"):
        # Make the MA equal to ``rhs_field`` level minus/plus a delta.
        ma_level = base_close
        bars = _flat_bars(ma_level, n)
        # Adjust the last bar's price field to satisfy the predicate.
        rhs_field = rhs.split(".", 1)[1]
        trigger_idx = n - _BARS_AFTER_TRIGGER - 1
        target_field_value = ma_level - 2.0 if op in (">", ">=") else ma_level + 2.0
        bar = bars[trigger_idx]
        bars[trigger_idx] = OHLCVBar(
            date="placeholder",
            open=bar.open,
            high=max(bar.high, target_field_value) if rhs_field == "high" else bar.high,
            low=min(bar.low, target_field_value) if rhs_field == "low" else bar.low,
            close=target_field_value if rhs_field == "close" else bar.close,
            volume=target_field_value if rhs_field == "volume" else bar.volume,
        )
        # Verify.
        df = _bars_to_df(bars)
        ma_series = (sma if lhs.name == "sma" else ema)(df["close"], int(lhs.param("period")))
        lv = ma_series.iloc[trigger_idx]
        rv = getattr(bars[trigger_idx], rhs_field)
        if isinstance(lv, float) and math.isfinite(lv) and _compare(float(lv), op, float(rv)):
            return bars, trigger_idx, None
    return None, 0, f"indicator_vs_priceref_unsupported:{lhs.name}_{rhs}"


# ---------------------------------------------------------------------------
# Comparison helper (mirrors the compiler's predicate eval semantics)
# ---------------------------------------------------------------------------


def _compare(lhs: float, op: str, rhs: float) -> bool:
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "==":
        return math.isclose(lhs, rhs, rel_tol=1e-6, abs_tol=1e-6)
    return False
