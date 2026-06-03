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

Only price-, P&L-, and signal-based exit rules are in scope.

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
from typing import Any, Callable, List, Literal, Optional, Tuple

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
from ..code_safety_ast import parse_strategy_source

# Bars/recipe knobs. Indicators with the largest lookback need the most
# bars; ``min_total_bars`` keeps short-lookback recipes well above the
# compiler's ``history_depth`` so warm-up never starves them.
_MIN_TOTAL_BARS = 80
_BARS_AFTER_TRIGGER = 5
_DECAY_SEARCH_ITERS = 12
_PROBE_SYMBOL_FALLBACK = "PROBE"
# Defensive trigger floor — most pandas indicators have natural NaN
# warmup (RSI period bars, etc.) but VWAP and other unbounded
# close-driven sequences can fire at bar 1. Floor all triggers at this
# value so probe runs land inside a realistic post-warmup window.
_COMPILER_MIN_WINDOW = 20


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

    ``post_clamp_verifier`` (when set) is a closure over the recipe's
    target predicate/condition that ``_stamp_dates`` invokes after
    ``_normalise_ohlc`` runs. Recipes that synthesise values close to the
    clamp floor (or that build OHLC arrangements OHLC normalisation may
    reshape) attach a verifier so a probe whose final post-clamp bars no
    longer satisfy the rule is marked unprobeable rather than emitted as
    a false critical against otherwise-correct strategy code.
    """

    rule_id: str
    rule_kind: Literal["entry", "stop_loss", "take_profit", "signal_exit"]
    symbol: str
    market_data: List[OHLCVBar] = field(default_factory=list)
    expected: Optional[ExpectedOutcome] = None
    trigger_bar_index: int = 0
    synthesizable: bool = True
    unprobeable_reason: Optional[str] = None
    post_clamp_verifier: Optional[Callable[[List[OHLCVBar], int], bool]] = field(
        default=None, repr=False, compare=False
    )


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
    """Rebuild a probe's bars with ascending calendar dates, clamp OHLC,
    and re-verify the recipe's predicate post-clamp.

    Recipe authors emit bars with placeholder ``date`` values for clarity —
    the actual dates are not meaningful, only their ordering. This pass
    assigns ``start_date + i days`` so downstream consumers (``BacktestConfig``,
    sandbox harness) get real, parseable date strings. It also clamps each
    bar's OHLC values so the downstream market-data preflight (which
    rejects nan_or_negative_prices and ohlc_violations) accepts the run.

    Normalisation can reshape values (close clamped to the floor;
    high/low re-derived to preserve invariants), which may invalidate a
    predicate the recipe verified against the pre-clamp series. If the
    probe attached a ``post_clamp_verifier``, this pass calls it and
    marks the probe unprobeable when the predicate no longer holds —
    surfacing as a warning rather than a false critical.
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
    if probe.post_clamp_verifier is not None:
        try:
            verified = probe.post_clamp_verifier(new_bars, probe.trigger_bar_index)
        except Exception:
            verified = False
        if not verified:
            return ProbeRun(
                rule_id=probe.rule_id,
                rule_kind=probe.rule_kind,
                symbol=probe.symbol,
                synthesizable=False,
                unprobeable_reason="post_clamp_predicate_violation",
            )
    return ProbeRun(
        rule_id=probe.rule_id,
        rule_kind=probe.rule_kind,
        symbol=probe.symbol,
        market_data=new_bars,
        expected=probe.expected,
        trigger_bar_index=probe.trigger_bar_index,
        synthesizable=probe.synthesizable,
        unprobeable_reason=probe.unprobeable_reason,
        post_clamp_verifier=probe.post_clamp_verifier,
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
    vol = (
        bar.volume
        if bar.volume is not None and math.isfinite(bar.volume) and bar.volume >= 0
        else 1.0
    )
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
    """Parse ``UNIVERSE = frozenset({...})`` (or assignment to ``self.UNIVERSE``,
    or an annotated assignment ``UNIVERSE: frozenset[str] = frozenset({...})``)
    from compiled-strategy source. Returns an empty frozenset on any failure.

    Plain ``Assign`` and annotated ``AnnAssign`` are both accepted because
    the deterministic compiler emits the bare form but hand-written or
    LLM-authored strategies often use the typed form, and a mismatched
    fallback to the ``"PROBE"`` sentinel would hit the strategy's
    universe-guard at the top of ``on_bar`` and produce a false critical.
    """
    if not code:
        return frozenset()
    try:
        tree = parse_strategy_source(code)
    except SyntaxError:
        return frozenset()
    for node in ast.walk(tree):
        target = None
        value = None
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            # Bare annotations like ``UNIVERSE: frozenset[str]`` (no
            # ``value``) carry no literal to parse — skip.
            if node.value is None:
                continue
            target = node.target
            value = node.value
        else:
            continue
        target_name = None
        if isinstance(target, ast.Name):
            target_name = target.id
        elif isinstance(target, ast.Attribute):
            target_name = target.attr
        if target_name != "UNIVERSE":
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
        ):
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


def _predicate_verifier(
    pred: Predicate,
) -> Callable[[List[OHLCVBar], int], bool]:
    """Return a closure that evaluates ``pred`` on a list of bars at index.

    Used as ``ProbeRun.post_clamp_verifier``: after :func:`_stamp_dates`
    normalises OHLC, we re-evaluate the predicate to catch cases where
    clamping invalidates a recipe that verified pre-clamp. ``cross_*``
    predicates evaluate over the (prev, curr) pair at ``index``;
    everything else evaluates at ``index`` directly.

    The closure returns ``False`` on any exception (malformed bars,
    unsupported predicate shape) so a verifier bug surfaces as
    "unprobeable" rather than a hard crash inside the synthesis loop.
    """

    def _verify(bars: List[OHLCVBar], idx: int) -> bool:
        if not bars or idx < 0 or idx >= len(bars):
            return False
        try:
            return _eval_predicate_at(pred, bars, idx)
        except Exception:
            return False

    return _verify


def _eval_predicate_at(pred: Predicate, bars: List[OHLCVBar], idx: int) -> bool:
    """Evaluate ``pred`` on ``bars`` at ``idx`` — mirror of the compiler's
    runtime predicate semantics, used only for post-clamp verification."""
    lhs, op, rhs = pred.lhs, pred.op, pred.rhs
    if op in ("cross_above", "cross_below"):
        if idx == 0:
            return False
        prev_l = _resolve_side(lhs, bars, idx - 1)
        cur_l = _resolve_side(lhs, bars, idx)
        prev_r = _resolve_side(rhs, bars, idx - 1)
        cur_r = _resolve_side(rhs, bars, idx)
        return _verify_cross(prev_l, cur_l, prev_r, cur_r, op)
    lhs_val = _resolve_side(lhs, bars, idx)
    rhs_val = _resolve_side(rhs, bars, idx)
    if lhs_val is None or rhs_val is None:
        return False
    if not math.isfinite(lhs_val) or not math.isfinite(rhs_val):
        return False
    return _compare(lhs_val, op, rhs_val)


def _resolve_side(side: Any, bars: List[OHLCVBar], idx: int) -> Optional[float]:
    """Resolve a Predicate side (PriceRef string, IndicatorRef, or number)
    to a float value at the given bar index. Returns None on indicator
    failure or unsupported side shape."""
    if isinstance(side, str):
        field_name = side.split(".", 1)[1] if side.startswith("bar.") else None
        if field_name is None:
            return None
        return float(getattr(bars[idx], field_name))
    if isinstance(side, IndicatorRef):
        value = _compute_indicator_at(side, bars, idx)
        return None if value is None else float(value)
    if isinstance(side, bool):
        return None
    if isinstance(side, (int, float)):
        return float(side)
    return None


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
        post_clamp_verifier=_predicate_verifier(rule.when),
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
    # Walk every entry rule and pick the first whose entry-prefix synthesises
    # *and* whose side is compatible with the exit rule under test. Hard-coding
    # ``entry_rules[0]`` would downgrade exit probes to unprobeable whenever
    # the first rule's predicate isn't synthesisable, or use the wrong side
    # (e.g. a long ``entry_rules[0]`` paired with a ``StopLossRule(basis=
    # "trailing_low")`` that the engine treats as a no-op for longs) — both
    # cases would hide real exit-rule regressions.
    last_failure = "no_compatible_entry_rule"
    for entry_rule in entry_rules:
        entry_bars, entry_trigger_idx, entry_reason = _synthesise_for_predicate(entry_rule.when)
        if entry_bars is None:
            last_failure = f"entry_prefix_not_synthesizable: {entry_reason}"
            continue
        entry_close = entry_bars[entry_trigger_idx].close
        entry_side = entry_rule.side
        probe = _build_exit_probe_with_prefix(
            rule=rule,
            rule_id=rule_id,
            kind=kind,
            symbol=symbol,
            entry_bars=entry_bars,
            entry_close=entry_close,
            entry_side=entry_side,
        )
        if probe.synthesizable:
            return probe
        last_failure = probe.unprobeable_reason or last_failure
    return ProbeRun(
        rule_id=rule_id,
        rule_kind=kind or "signal_exit",
        symbol=symbol,
        synthesizable=False,
        unprobeable_reason=last_failure,
    )


def _build_exit_probe_with_prefix(
    *,
    rule: Any,
    rule_id: str,
    kind: Optional[str],
    symbol: str,
    entry_bars: List[OHLCVBar],
    entry_close: float,
    entry_side: str,
) -> ProbeRun:
    """Build the exit probe given a successfully-synthesised entry prefix.

    Returns ``synthesizable=False`` with a descriptive reason when the
    chosen ``(rule, entry_side)`` pair has no valid tail (e.g. a
    ``trailing_low`` stop on a long entry — the engine's no-op case).
    Callers iterate across entry rules and pick the first probe that
    comes back ``synthesizable=True``.
    """
    if isinstance(rule, StopLossRule):
        tail, tail_trigger_offset = _synthesise_stop_loss_tail(rule, entry_close, entry_side)
        if tail is None:
            return ProbeRun(
                rule_id=rule_id,
                rule_kind="stop_loss",
                symbol=symbol,
                synthesizable=False,
                unprobeable_reason=f"stop_loss_tail_not_synthesizable_for_side={entry_side}",
            )
        full_bars = _stitch(entry_bars, tail)
        return ProbeRun(
            rule_id=rule_id,
            rule_kind="stop_loss",
            symbol=symbol,
            market_data=full_bars,
            expected=ExpectedOutcome(kind="exit", exit_reason_contains="stop_loss"),
            trigger_bar_index=len(entry_bars) + tail_trigger_offset,
            post_clamp_verifier=_stop_loss_verifier(rule, entry_close, entry_side),
        )
    if isinstance(rule, TakeProfitRule):
        tail, tail_trigger_offset = _synthesise_take_profit_tail(rule, entry_close, entry_side)
        if tail is None:
            return ProbeRun(
                rule_id=rule_id,
                rule_kind="take_profit",
                symbol=symbol,
                synthesizable=False,
                unprobeable_reason=f"take_profit_tail_not_synthesizable_for_side={entry_side}",
            )
        full_bars = _stitch(entry_bars, tail)
        return ProbeRun(
            rule_id=rule_id,
            rule_kind="take_profit",
            symbol=symbol,
            market_data=full_bars,
            expected=ExpectedOutcome(kind="exit", exit_reason_contains="take_profit"),
            trigger_bar_index=len(entry_bars) + tail_trigger_offset,
            post_clamp_verifier=_take_profit_verifier(rule, entry_close, entry_side),
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
            post_clamp_verifier=_predicate_verifier(rule.when),
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


def _stop_loss_verifier(
    rule: StopLossRule, entry_close: float, entry_side: str
) -> Callable[[List[OHLCVBar], int], bool]:
    """Closure that re-checks the engine's stop-loss trigger condition on
    the post-clamp trigger bar. Used by :func:`_stamp_dates` so a probe
    whose adversarial low/high was clipped by ``_normalise_ohlc`` doesn't
    ship a non-triggering bar to the sandbox."""
    pct = rule.pct

    def _verify(bars: List[OHLCVBar], idx: int) -> bool:
        if not bars or idx < 0 or idx >= len(bars):
            return False
        bar = bars[idx]
        if entry_side == "long":
            floor = entry_close * (1.0 - pct)
            return bar.low <= floor
        ceiling = entry_close * (1.0 + pct)
        return bar.high >= ceiling

    return _verify


def _take_profit_verifier(
    rule: TakeProfitRule, entry_close: float, entry_side: str
) -> Callable[[List[OHLCVBar], int], bool]:
    """Closure that re-checks the engine's take-profit trigger condition
    on the post-clamp trigger bar."""
    pct = rule.pct

    def _verify(bars: List[OHLCVBar], idx: int) -> bool:
        if not bars or idx < 0 or idx >= len(bars):
            return False
        bar = bars[idx]
        if entry_side == "long":
            target = entry_close * (1.0 + pct)
            return bar.high >= target
        target = entry_close * (1.0 - pct)
        return bar.low <= target

    return _verify


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
    if (
        isinstance(lhs, IndicatorRef)
        and isinstance(rhs, (int, float))
        and not isinstance(rhs, bool)
    ):
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
    """Generate bars where the bar's price field satisfies ``lhs op rhs``.

    Bails out (unprobeable) when the synthesised values would be clamped
    by :func:`_normalise_ohlc` (every OHLC value floored to ``_MIN_PRICE``).
    Verifying on raw bars and shipping clamped bars to the sandbox would
    produce a false probe failure (e.g. ``bar.close < 0.005`` synthesised
    as ``-0.995`` but seen as ``0.01`` post-clamp).
    """
    n = max(min_bars, 30)
    trigger_idx = n - _BARS_AFTER_TRIGGER - 1
    bars: List[OHLCVBar] = []
    field_name = lhs.split(".", 1)[1]  # "bar.close" -> "close"
    # Baseline closes that don't satisfy the predicate.
    baseline = rhs - 5.0 if op in (">", ">=") else rhs + 5.0
    if op == ">":
        trigger_value = rhs + 1.0
    elif op == ">=":
        trigger_value = rhs + 0.5
    elif op == "<":
        trigger_value = rhs - 1.0
    elif op == "<=":
        trigger_value = rhs - 0.5
    elif op == "==":
        trigger_value = rhs
    else:
        return None, 0, f"unsupported_priceref_op:{op}"
    # ``_normalise_ohlc`` floors every OHLC value to ``_MIN_PRICE`` and may
    # round high/low up/down around the field's adjacent values; the
    # critical case is the trigger value itself becoming ``_MIN_PRICE`` and
    # no longer satisfying the predicate. Refuse the probe in that case.
    if trigger_value < _MIN_PRICE and not _compare(_MIN_PRICE, op, rhs):
        return None, 0, "priceref_value_below_clamp_floor"
    if baseline < _MIN_PRICE and field_name != "volume":
        return None, 0, "priceref_baseline_below_clamp_floor"
    for i in range(n):
        if i == trigger_idx:
            bars.append(_bar_with_field(field_name, trigger_value))
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
        return OHLCVBar(
            date="placeholder", open=100.0, high=100.5, low=99.5, close=100.0, volume=value
        )
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
    # ``bar.volume`` is a valid PriceRef per the DSL but doesn't fit the
    # synthesiser's OHLC default model (volume is on a different scale and
    # rarely comparable against prices). Treat volume-mixed predicates as
    # unprobeable rather than risking a KeyError or producing meaningless
    # synthetic bars.
    if "bar.volume" in (lhs, rhs):
        return None, 0, "priceref_volume_comparison_unprobeable"
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
    # Some PriceRef relations are structurally unsatisfiable under OHLC
    # invariants (e.g. ``bar.high < bar.low``); :func:`_bar_with_priceref_relation`
    # tries to adjust the lhs field but ``_normalise_ohlc`` then restores
    # the invariant, leaving the predicate false. Verify here so the recipe
    # marks the probe unprobeable instead of shipping a non-triggering bar.
    trigger_bar = _normalise_ohlc(bars[trigger_idx])
    lhs_val = getattr(trigger_bar, lhs_field)
    rhs_val = getattr(trigger_bar, rhs_field)
    if not _compare(float(lhs_val), op, float(rhs_val)):
        return None, 0, "priceref_vs_priceref_invariant_conflict"
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
    # Refine trigger_idx to the earliest bar the predicate actually holds at —
    # the asserter requires entry_date >= trigger_date, and a correctly-built
    # strategy opens at the rule's first-fire bar, not at the recipe's
    # constructed end-of-window bar.
    earliest = _earliest_indicator_satisfying_index(ref, bars, op, rhs)
    if earliest is not None:
        trigger_idx = earliest
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
        # Miss → push the step LARGER (steeper movement in the chosen
        # direction). For target_below=True a bigger step deepens the
        # decline and drops RSI further; for target_below=False a bigger
        # step steepens the rise and lifts RSI further. The previous
        # branch did `hi = mid` for target_below=False, contracting toward
        # the smallest step — exactly the wrong direction — so high RSI
        # thresholds (op `>`) were systematically unreachable.
        lo = mid
    # Last-chance: use the steepest step (hi) regardless of direction.
    closes = closes_for(hi)
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


# ---------------------------------------------------------------------------
# Forcing-sequence builders used by the unified cross search.
# Each builder returns ``List[OHLCVBar]`` of length ``n`` with ``_MIN_PRICE``
# clamping. Builders are intentionally indicator-agnostic — the cross search
# scans the resulting bars and lets the indicator math decide where the cross
# fires.
# ---------------------------------------------------------------------------


def _monotonic_trend_bars(n: int, base_close: float, slope: float) -> List[OHLCVBar]:
    """Steady price drift at ``slope`` per bar — drives MACD spreads, EMA divergence."""
    closes = [max(_MIN_PRICE, base_close + slope * i) for i in range(n)]
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=max(_MIN_PRICE, c - 0.5),
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _volume_regime_change_bars(
    n: int,
    base_close: float,
    base_volume: float,
    volume_slope: float,
    reverse: bool = False,
) -> List[OHLCVBar]:
    """Volume ramps down for the first half then up for the second (or vice
    versa with ``reverse=True``). Mirrors `_regime_change_bars` but operates
    on the volume column, holding close steady.

    Used when both crossed indicators read from ``source="volume"`` — the
    shorter-period volume SMA/EMA crosses the longer-period one at the
    regime transition.
    """
    midpoint = n // 2
    volumes: List[float] = []
    for i in range(n):
        if (i < midpoint) != reverse:
            volumes.append(max(1.0, base_volume - volume_slope * i))
        else:
            offset = max(1.0, volume_slope * (i - midpoint + 1))
            prev = volumes[-1] if volumes else base_volume
            volumes.append(max(1.0, prev + offset))
    return [
        OHLCVBar(
            date="placeholder",
            open=base_close,
            high=base_close + 0.5,
            low=max(_MIN_PRICE, base_close - 0.5),
            close=base_close,
            volume=v,
        )
        for v in volumes
    ]


def _close_and_range_ramp_bars(
    n: int,
    max_amplitude: float,
    direction: str = "up",
    reverse: bool = False,
) -> List[OHLCVBar]:
    """Bars where both close and high-low range ramp from ε to ``max_amplitude``.

    Used for ``priceref cross ATR`` — the close must vary in the ATR
    magnitude (typically much smaller than typical close prices) for the
    cross to actually fire. With ``direction="up"`` close rises from ε to
    ``max_amplitude * 1.5``; with ``"down"`` it falls. ``reverse=True``
    inverts the ramp time-direction.
    """
    sign = 1.0 if direction == "up" else -1.0
    base = max_amplitude * 0.75
    bars: List[OHLCVBar] = []
    for i in range(n):
        frac = i / max(1, n - 1)
        if reverse:
            frac = 1.0 - frac
        amp = max(0.1, max_amplitude * frac)
        close = max(_MIN_PRICE, base + sign * (max_amplitude * (frac - 0.5)))
        bars.append(
            OHLCVBar(
                date="placeholder",
                open=close,
                high=close + amp,
                low=max(_MIN_PRICE, close - amp),
                close=close,
                volume=1_000_000.0,
            )
        )
    return bars


def _widening_range_bars(
    n: int, base_close: float, max_amplitude: float, reverse: bool = False
) -> List[OHLCVBar]:
    """Bars whose high-low range widens linearly from ε to ``max_amplitude``.

    Drives ATR from a low baseline up through any threshold (or down, with
    ``reverse=True``). Close is held flat at ``base_close``; only the per-bar
    high/low spread changes.
    """
    bars: List[OHLCVBar] = []
    for i in range(n):
        frac = i / max(1, n - 1)
        if reverse:
            frac = 1.0 - frac
        amp = max(0.1, max_amplitude * frac)
        bars.append(
            OHLCVBar(
                date="placeholder",
                open=base_close,
                high=base_close + amp,
                low=max(_MIN_PRICE, base_close - amp),
                close=base_close,
                volume=1_000_000.0,
            )
        )
    return bars


def _two_phase_trend_bars(
    n: int,
    base_close: float,
    phase1_slope: float,
    phase2_slope: float,
    phase1_frac: float = 0.5,
) -> List[OHLCVBar]:
    """Trend in one direction for ``phase1_frac`` of the bars, then trend in
    the opposite direction for the rest.

    Used to seed indicators on the far side of a signed threshold. For
    example, ``MACD cross_above -50`` needs MACD to start *below* -50
    before rising through it — a single-phase up-trend starts MACD at 0
    (already above -50). Phase 1 with steep negative slope drives MACD to
    below -50; phase 2 with positive slope brings it back through any
    threshold up to ~ phase2_slope × spread / 2.
    """
    p1 = max(1, int(n * phase1_frac))
    closes: List[float] = [max(_MIN_PRICE, base_close + phase1_slope * i) for i in range(p1)]
    phase1_end = closes[-1] if closes else base_close
    for i in range(n - p1):
        closes.append(max(_MIN_PRICE, phase1_end + phase2_slope * (i + 1)))
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=max(_MIN_PRICE, c - 0.5),
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _volume_trend_bars(
    n: int, base_close: float, base_volume: float, volume_slope: float
) -> List[OHLCVBar]:
    """Vary the volume series while holding close flat.

    For indicators with ``source="volume"`` — ``_compute_indicator_series``
    reads from the volume column, so price-varying builders produce a flat
    indicator. This builder is symmetric: it drives the volume series
    monotonically while keeping prices steady at ``base_close``.
    """
    return [
        OHLCVBar(
            date="placeholder",
            open=base_close,
            high=base_close + 0.5,
            low=max(_MIN_PRICE, base_close - 0.5),
            close=base_close,
            volume=max(1.0, base_volume + volume_slope * i),
        )
        for i in range(n)
    ]


def _volume_peak_then_trough_bars(
    n: int,
    base_close: float,
    base_volume: float,
    amplitude: float,
    reverse: bool = False,
) -> List[OHLCVBar]:
    """Symmetric two-phase volume sequence: first half ramps from
    ``base_volume`` to ``base_volume + amplitude``, second half ramps from
    ``base_volume + amplitude`` to ``base_volume - amplitude``. With
    ``reverse=True``: first half drops to ``base_volume - amplitude``, then
    rises to ``base_volume + amplitude``.

    Drives EMA-difference indicators (MACD line, signal, histogram) through
    BOTH a positive peak and a negative trough, so cross thresholds on
    either side of zero are reachable from the appropriate side.

    Used by the MACD branch of :func:`_builders_for_indicator_number` —
    :func:`_volume_regime_change_bars` is asymmetric (the rise phase under
    ``reverse=True`` only adds 1.0/bar because its offset clamps at the
    floor for negative `i - midpoint`), so MACD never reaches a meaningful
    positive peak from that helper.
    """
    midpoint = max(1, n // 2)
    second_half = max(1, n - midpoint)
    volumes: List[float] = []
    for i in range(n):
        if i < midpoint:
            frac = i / midpoint
            delta = amplitude * frac if not reverse else -amplitude * frac
        else:
            j = i - midpoint
            frac = j / second_half
            if not reverse:
                delta = amplitude - 2.0 * amplitude * frac
            else:
                delta = -amplitude + 2.0 * amplitude * frac
        volumes.append(max(1.0, base_volume + delta))
    return [
        OHLCVBar(
            date="placeholder",
            open=base_close,
            high=base_close + 0.5,
            low=max(_MIN_PRICE, base_close - 0.5),
            close=base_close,
            volume=v,
        )
        for v in volumes
    ]


def _close_ramp_with_priced_volume(n: int, base_close: float, close_slope: float) -> List[OHLCVBar]:
    """Bars where the volume column is anchored at ``base_close`` (price
    magnitude, NOT typical 1e6 scale) and close trends linearly at
    ``close_slope`` from ``base_close - n*|slope|/2``.

    Used by the volume-source SMA/EMA branch of
    :func:`_builders_for_priceref_cross`: when ``bar.close cross_*
    SMA(source="volume")`` the runtime indicator reads df["volume"] —
    holding volume at 1e6 anchors SMA(volume) at 1e6 forever and close
    (~base_close) can never cross it. Putting volume in price-magnitude
    range gives SMA(volume) ≈ base_close while close ramps through it.
    """
    midpoint = n // 2
    closes = [max(_MIN_PRICE, base_close + (i - midpoint) * close_slope) for i in range(n)]
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=max(_MIN_PRICE, c - 0.5),
            close=c,
            volume=base_close,
        )
        for c in closes
    ]


def _step_bars(
    n: int, low_level: float, high_level: float, step_frac: float = 0.5
) -> List[OHLCVBar]:
    """Two flat regimes joined at ``step_frac``. The SMA / Bollinger middle
    band ramps linearly between ``low_level`` and ``high_level`` over the
    window, crossing any threshold in between.

    Mirrors the inline shape already used by ``_synth_cross_indicator_number``
    for SMA/EMA so the Bollinger middle band (which is also an SMA) can
    delegate to it for threshold-aware crosses.
    """
    step_idx = max(1, int(n * step_frac))
    closes = [max(_MIN_PRICE, low_level)] * step_idx + [max(_MIN_PRICE, high_level)] * (
        n - step_idx
    )
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=max(_MIN_PRICE, c - 0.5),
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _quiet_then_breakout(n: int, base_close: float, direction: str) -> List[OHLCVBar]:
    """Long quiet history at ``base_close`` then a 5-bar breakout. Drives
    Bollinger bands wide and pushes price across the upper / lower band.

    ``direction`` is ``"up"`` (price spikes above ``base_close``) or
    ``"down"`` (price drops below it).
    """
    sign = 1.0 if direction == "up" else -1.0
    quiet_len = max(1, n - 5)
    closes = [base_close] * quiet_len + [
        max(_MIN_PRICE, base_close + sign * 10.0 * i) for i in range(1, n - quiet_len + 1)
    ]
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=max(_MIN_PRICE, c - 0.5),
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _high_vol_then_quiet(
    n: int, base_close: float, *, vol_frac: float = 0.5, amplitude: float = 5.0
) -> List[OHLCVBar]:
    """High-volatility alternating bars followed by a flat phase.

    Drives Bollinger bands wide during the volatile phase (upper band ≈
    base_close + 2σ) then collapses them when prices flatten (σ → 0, upper
    → middle ≈ base_close). The natural shape for ``cross_below`` of an
    upper band against a threshold near ``base_close`` — the band drops
    through the threshold as σ shrinks.
    """
    vol_len = max(1, int(n * vol_frac))
    closes: List[float] = []
    for i in range(vol_len):
        sign = 1 if i % 2 == 0 else -1
        closes.append(max(_MIN_PRICE, base_close + sign * amplitude))
    for _ in range(n - vol_len):
        closes.append(base_close)
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 1.0,
            low=max(_MIN_PRICE, c - 1.0),
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _quiet_then_trend_bars(
    n: int,
    base_close: float,
    *,
    direction: str = "up",
    quiet_frac: float = 0.5,
    trend_first: bool = False,
) -> List[OHLCVBar]:
    """Low-volatility flat history followed by a sustained directional trend.

    Drives indicators that measure trend strength (ADX) or directional
    momentum (Stochastic) from a low baseline through any threshold. Plain
    monotonic-trend bars pin ADX to 100 from the start because there is no
    flat baseline; pure regime change disrupts the trend repeatedly and ADX
    stays high. This builder produces the only sequence where ADX crosses
    25 from below.

    When ``trend_first=True`` the order is reversed (trend phase first, then
    quiet phase) so a trend-strength indicator climbs to a high value and
    then decays — needed for ``cross_below`` style predicates.
    """
    sign = 1.0 if direction == "up" else -1.0
    if trend_first:
        trend_len = max(1, int(n * (1.0 - quiet_frac)))
        closes = [max(_MIN_PRICE, base_close + sign * (i + 1) * 1.0) for i in range(trend_len)]
        trend_end = closes[-1] if closes else base_close
        for i in range(n - trend_len):
            closes.append(max(_MIN_PRICE, trend_end + (i % 2 - 0.5) * 0.2))
    else:
        quiet_len = max(1, int(n * quiet_frac))
        closes = [max(_MIN_PRICE, base_close + (i % 2 - 0.5) * 0.2) for i in range(quiet_len)]
        for i in range(n - quiet_len):
            closes.append(max(_MIN_PRICE, base_close + sign * (i + 1) * 1.0))
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 1.0,
            low=max(_MIN_PRICE, c - 1.0),
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _regime_change_bars(
    n: int,
    base_close: float,
    *,
    drop_rate: float = 0.98,
    rise_rate: float = 1.03,
    midpoint_frac: float = 0.5,
) -> List[OHLCVBar]:
    """Declining regime then a rising regime. Causes adjacent-period moving
    averages and other smoothed indicators to cross at the regime change.

    Extracted from the inline construction previously in
    ``_synth_cross_indicator_indicator``. Parameters allow the search to try
    different midpoints / decay rates when the default doesn't produce a cross.
    """
    midpoint = int(n * midpoint_frac)
    closes: List[float] = []
    for i in range(n):
        if i < midpoint:
            closes.append(base_close * (drop_rate**i))
        else:
            closes.append(closes[-1] * rise_rate)
    closes = [max(_MIN_PRICE, c) for c in closes]
    return [
        OHLCVBar(
            date="placeholder",
            open=c,
            high=c + 0.5,
            low=max(_MIN_PRICE, c - 0.5),
            close=c,
            volume=1_000_000.0,
        )
        for c in closes
    ]


def _verify_indicator_vs_number(
    bars: List[OHLCVBar], ref: IndicatorRef, op: str, rhs: float, idx: int
) -> bool:
    value = _compute_indicator_at(ref, bars, idx)
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return False
    return _compare(value, op, rhs)


def _earliest_indicator_satisfying_index(
    ref: IndicatorRef, bars: List[OHLCVBar], op: str, rhs: float
) -> Optional[int]:
    """Scan the bar series for the first index where ``indicator(bars) op rhs``
    holds. Used to align the probe's ``trigger_bar_index`` with the bar a
    correctly-built strategy would actually open on (the rule's first-fire
    bar). Returns ``None`` if no bar satisfies the predicate.

    Computes the indicator as a single pandas batch via
    :func:`_compute_indicator_series` and walks the resulting series — O(n)
    pandas work + O(n) Python iteration. The earlier per-bar
    :func:`_compute_indicator_at` rebuild rebuilt the full DataFrame and
    recomputed the full indicator series on every iteration (O(n²) pandas
    work) and contributed measurably to the synthesis-loop hot path under
    multi-rule specs with slow indicators.
    """
    series = _compute_indicator_series(ref, bars)
    if series is None:
        return None
    for i, value in enumerate(series.tolist()):
        if value is None:
            continue
        f = float(value)
        if not math.isfinite(f):
            continue
        if _compare(f, op, rhs):
            return i
    return None


def _compute_indicator_series(ref: IndicatorRef, bars: List[OHLCVBar]) -> Optional[pd.Series]:
    """Compute ``ref`` over the synthetic bar series as a full ``pd.Series``.

    The cross-search engine calls this once per (bars, ref) pair instead of
    invoking :func:`_compute_indicator_at` per bar, which would re-build the
    DataFrame and recompute the indicator on every iteration. Returns ``None``
    for unrecognised indicator names.
    """
    df = _bars_to_df(bars)
    series = _series_for_source(df, ref.source)
    if ref.name == "sma":
        return sma(series, int(ref.param("period")))
    if ref.name == "ema":
        return ema(series, int(ref.param("period")))
    if ref.name == "rsi":
        return rsi(series, int(ref.param("period")))
    if ref.name == "macd":
        line, signal_, hist = macd(
            series,
            int(ref.param("fast")),
            int(ref.param("slow")),
            int(ref.param("signal")),
        )
        output = ref.param("output")
        return {"macd": line, "signal": signal_, "histogram": hist}[output]
    if ref.name == "bollinger":
        upper, middle, lower = bollinger_bands(
            series, int(ref.param("period")), float(ref.param("num_std"))
        )
        band = ref.param("band")
        return {"upper": upper, "middle": middle, "lower": lower}[band]
    if ref.name == "atr":
        return atr(df["high"], df["low"], df["close"], int(ref.param("period")))
    if ref.name == "adx":
        return adx(df["high"], df["low"], df["close"], int(ref.param("period")))
    if ref.name == "stochastic":
        k, d = stochastic(
            df["high"],
            df["low"],
            df["close"],
            int(ref.param("k_period")),
            int(ref.param("d_period")),
        )
        output = ref.param("output")
        return {"k": k, "d": d}[output]
    if ref.name == "vwap":
        return vwap(df["high"], df["low"], df["close"], df["volume"])
    return None


_PRICEREF_TO_FIELD = {
    "bar.close": "close",
    "bar.high": "high",
    "bar.low": "low",
    "bar.volume": "volume",
}


def _resolve_side_series(
    side: Any,
    bars: List[OHLCVBar],
    df: pd.DataFrame,
    depth: Optional[int] = None,  # kept for backward-compat with callers
) -> Optional[pd.Series]:
    """Resolve a predicate side (``IndicatorRef`` / bar-field string / float)
    to a full ``pd.Series`` aligned with ``bars``.

    Returns ``None`` when the side references an unknown indicator or
    bar-field literal — the search treats that builder as failed and moves on.

    Uses :func:`_compute_indicator_series` (pandas batch helpers from
    ``executor/indicators.py``) — this is what the engine itself uses at
    runtime: ``trading_service/service.py:_evaluate_entry_rules_pred`` calls
    into ``predicate_evaluator.evaluate_entry_rules`` which materialises
    indicator values through :class:`StreamingHistoryView.indicator`, which
    in turn delegates to ``compute_indicator_series`` in
    ``predicate_evaluator.py`` (the pandas helpers). The post-clamp verifier
    at :func:`_eval_predicate_at` also resolves indicator values via
    :func:`_compute_indicator_at` (pandas). Aligning the synthesizer to the
    same path keeps the reported trigger consistent with both the engine and
    the post-clamp verifier.

    ``depth`` is kept on the signature for backward compatibility but is
    unused — the pandas helpers compute against the full ``bars`` series and
    the runtime's ``StreamingHistoryView`` is bounded at 500 bars, well
    above any synthesized forcing sequence.
    """
    del depth  # noqa — preserved as keyword arg for callers
    if isinstance(side, IndicatorRef):
        return _compute_indicator_series(side, bars)
    if isinstance(side, str):
        field_name = _PRICEREF_TO_FIELD.get(side)
        if field_name is None:
            return None
        return df[field_name]
    if isinstance(side, (int, float)) and not isinstance(side, bool):
        return pd.Series([float(side)] * len(bars))
    return None


def _search_cross(
    lhs: Any,
    op: str,
    rhs: Any,
    base_close: float,
    min_bars: int,
    builders: List[Callable[[], List[OHLCVBar]]],
    warmup: int = 1,
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Try each forcing-sequence ``builder`` and scan the resulting bars for
    the first index where ``lhs op rhs`` crosses according to :func:`_verify_cross`.

    Returns ``(bars, trigger_idx, None)`` on the first hit. If no builder
    produces a cross, returns ``(None, 0, reason)`` — the bare string
    ``"cross_not_found_in_window"`` when at least one builder successfully
    resolved both sides and exhausted the scan, otherwise
    ``"cross_side_unresolved"`` when every builder failed at the resolve
    stage (an unknown indicator name or unmapped priceref). The "resolved
    but no cross" diagnostic is preferred over "side unresolved" — earlier
    versions overwrote the former with the latter when the spike builder
    succeeded and the OHLC fallback failed resolution, masking the
    informative diagnosis.

    Indicator values are resolved through :func:`_resolve_side_series` which
    uses the same pandas helpers ``predicate_evaluator.evaluate_entry_rules``
    consults at runtime — keeping the synthesizer's trigger consistent with
    the post-clamp verifier and the engine. The scan is floored at
    ``_COMPILER_MIN_WINDOW`` (defensive — most pandas indicators have
    natural NaN warmup, but VWAP / unbounded close-driven sequences could
    otherwise fire at bar 1).
    """
    runtime_warmup = max(warmup, _COMPILER_MIN_WINDOW)
    any_resolved = False
    for builder in builders:
        bars = builder()
        df = _bars_to_df(bars)
        lhs_series = _resolve_side_series(lhs, bars, df)
        rhs_series = _resolve_side_series(rhs, bars, df)
        if lhs_series is None or rhs_series is None:
            continue
        any_resolved = True
        n = min(len(lhs_series), len(rhs_series))
        for idx in range(max(runtime_warmup, 1), n):
            if _verify_cross(
                lhs_series.iloc[idx - 1],
                lhs_series.iloc[idx],
                rhs_series.iloc[idx - 1],
                rhs_series.iloc[idx],
                op,
            ):
                return bars, idx, None
    reason = "cross_not_found_in_window" if any_resolved else "cross_side_unresolved"
    return None, 0, reason


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
            df["high"],
            df["low"],
            df["close"],
            int(ref.param("k_period")),
            int(ref.param("d_period")),
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
        period = (
            int(ref.param("period"))
            if "period" in ref.params or ref.name in ("rsi", "atr", "adx")
            else 20
        )
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
    if (
        isinstance(lhs, IndicatorRef)
        and isinstance(rhs, (int, float))
        and not isinstance(rhs, bool)
    ):
        return _synth_cross_indicator_number(lhs, op, float(rhs), base_close, min_bars)
    return None, 0, f"unsupported_cross_shape:{type(lhs).__name__}_{type(rhs).__name__}"


def _synth_cross_priceref_indicator(
    lhs: str, op: str, rhs: IndicatorRef, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Construct a bar sequence where ``lhs op rhs`` fires for the indicator
    named on ``rhs``.

    SMA / EMA: keep the historical flat-history + single-trigger-bar shape so
    the trigger index lands deterministically near the end of the window.
    Other indicators delegate to the unified :func:`_search_cross` engine with
    an indicator-specific catalogue of forcing-sequence builders.
    """
    if lhs not in _PRICEREF_TO_FIELD:
        return None, 0, f"cross_against_unsupported_priceref:{lhs}"

    if rhs.name in ("sma", "ema") and lhs == "bar.close" and rhs.source == "close":
        # Preserved exactly when the MA reads close (the default source):
        # long flat history + one trigger bar that pushes the close above
        # / below the moving average. For non-close sources the trigger
        # bar's close jump doesn't move the indicator (e.g. SMA(volume,N)
        # stays pinned at 1e6 because volume is held flat), so the
        # post-clamp verifier rejects the candidate trigger and the rule
        # is silently marked unprobeable. Fall through to the source-aware
        # generic search below in that case.
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
        df = _bars_to_df(bars)
        ma_series = (sma if rhs.name == "sma" else ema)(df["close"], int(rhs.param("period")))
        prev_close = df["close"].iloc[trigger_idx - 1]
        cur_close = df["close"].iloc[trigger_idx]
        prev_ma = ma_series.iloc[trigger_idx - 1]
        cur_ma = ma_series.iloc[trigger_idx]
        if not _verify_cross(prev_close, cur_close, prev_ma, cur_ma, op):
            return None, 0, "cross_priceref_indicator_verification_failed"
        return bars, trigger_idx, None

    # bar.volume needs a volume-varying builder catalogue — the OHLC-only
    # builders below hold volume flat at 1_000_000 so `bar.volume` (1e6)
    # never crosses any indicator value (volume-source indicators sit at
    # 1e6, VWAP / close-source indicators sit at base_close ≈ 100).
    if lhs == "bar.volume":
        builders = _builders_for_volume_priceref_cross(rhs, op, base_close, min_bars)
        warmup = _warmup_for_indicator(rhs)
        return _search_cross(lhs, op, rhs, base_close, min_bars, builders, warmup=warmup)

    # bar.high / bar.low against close-tracking indicators (Bollinger bands,
    # VWAP, ATR) need a forcing sequence that decouples LHS from close — the
    # OHLC catalogue holds high = close + 0.5 and low = close - 0.5, so any
    # close-tracking RHS moves in lockstep with LHS and the cross never
    # fires. Prepend a high-spike / low-drop catalogue that holds close flat
    # (RHS anchored at base_close) and spikes the LHS column at the trigger.
    # The OHLC catalogue is still tried as a fallback for indicators that
    # already cross naturally with close-correlated high/low (e.g. SMA, RSI).
    if lhs in ("bar.high", "bar.low"):
        builders = _builders_for_high_low_priceref_cross(rhs, op, base_close, min_bars, lhs)
        warmup = _warmup_for_indicator(rhs)
        return _search_cross(lhs, op, rhs, base_close, min_bars, builders, warmup=warmup)

    # Generic path: choose a builder catalogue per indicator and let the
    # search find the first bar where the cross actually fires.
    builders = _builders_for_priceref_cross(rhs, op, base_close, min_bars)
    warmup = _warmup_for_indicator(rhs)
    return _search_cross(lhs, op, rhs, base_close, min_bars, builders, warmup=warmup)


def _high_low_spike_bars(
    n: int,
    base_close: float,
    spike_idx: int,
    field: str,
    spike_value: float,
) -> List[OHLCVBar]:
    """Flat-close, narrow-range bars where ``field`` (``"high"`` or ``"low"``)
    sits anchored at ``base_close`` throughout warmup and steps to
    ``spike_value`` only at ``spike_idx``.

    Holding close flat at ``base_close`` keeps every close-derived RHS
    indicator value anchored:
    - SMA(close,N) = base_close.
    - Bollinger middle = base_close; σ = 0; upper/lower = base_close.
    - VWAP = base_close (cumsum(close * volume) / cumsum(volume) collapses).
    - ATR is built from true-range = max(high-low, |high-prev_close|,
      |low-prev_close|); narrow flat ranges keep ATR near zero until the
      spike, where it jumps but the LHS spike still dominates.

    For ``field="high"``: warmup high = base_close (so prev_high ≤ any
    indicator value ≥ base_close); cur_high = ``spike_value`` (≥
    base_close * 2 in the catalogue below). For ``field="low"``: mirror.

    Used exclusively from :func:`_builders_for_high_low_priceref_cross`.
    """
    # Every non-spike bar is a doji at exactly ``base_close`` — high == low
    # == open == close. This keeps the typical-price-based RHS (VWAP, which
    # averages (H + L + C) / 3) anchored at exactly base_close instead of
    # base_close - epsilon, so the prev edge of the cross (prev_high ≤
    # prev_vwap or prev_low ≥ prev_vwap) is satisfied. ATR / Bollinger σ
    # also stay at zero rather than a small positive margin from a ±0.01
    # range.
    bars: List[OHLCVBar] = []
    for i in range(n):
        if i == spike_idx and field == "high":
            # Honour the caller's spike direction. For cross_above spike_value
            # is > base_close (upward spike); for cross_below it is <
            # base_close (downward spike), so set high to spike_value
            # directly — the older `max(spike_value, base_close)` clamp made
            # cross_below structurally impossible by floor-clamping high.
            close_at_spike = (
                min(base_close, spike_value) if spike_value < base_close else base_close
            )
            high_at_spike = max(spike_value, close_at_spike)
            low_at_spike = max(_MIN_PRICE, min(close_at_spike, spike_value))
            bars.append(
                OHLCVBar(
                    date="placeholder",
                    open=close_at_spike,
                    high=high_at_spike,
                    low=low_at_spike,
                    close=close_at_spike,
                    volume=1_000_000.0,
                )
            )
        elif i == spike_idx and field == "low":
            # Same fix for low — honour the spike direction. The older
            # `min(spike_value, base_close)` clamp made cross_above
            # structurally impossible by ceiling-clamping low.
            close_at_spike = (
                max(base_close, spike_value) if spike_value > base_close else base_close
            )
            low_at_spike = max(_MIN_PRICE, min(spike_value, close_at_spike))
            high_at_spike = max(spike_value, close_at_spike)
            bars.append(
                OHLCVBar(
                    date="placeholder",
                    open=close_at_spike,
                    high=high_at_spike,
                    low=low_at_spike,
                    close=close_at_spike,
                    volume=1_000_000.0,
                )
            )
        else:
            bars.append(
                OHLCVBar(
                    date="placeholder",
                    open=base_close,
                    high=base_close,
                    low=base_close,
                    close=base_close,
                    volume=1_000_000.0,
                )
            )
    return bars


def _builders_for_high_low_priceref_cross(
    rhs: IndicatorRef,
    op: str,
    base_close: float,
    min_bars: int,
    lhs: str,
) -> List[Callable[[], List[OHLCVBar]]]:
    """Forcing-sequence catalogue for ``bar.high cross_* indicator`` and
    ``bar.low cross_* indicator``.

    The OHLC catalogue in :func:`_builders_for_priceref_cross` couples high
    and low to close (``high = close + 0.5``, ``low = close - 0.5``), so for
    indicators that track close (Bollinger bands, VWAP, ATR on quiet data)
    the LHS moves in lockstep with the RHS and the cross never fires. The
    spike builder below holds close flat so the RHS stays anchored at
    ``base_close``, then jumps LHS independently at the trigger bar.

    The OHLC builders are appended as fallbacks for indicators (SMA, RSI)
    that *do* cross naturally with close-correlated high/low — those probes
    already work without the spike shape.
    """
    warmup = _warmup_for_indicator(rhs)
    n = max(min_bars, warmup + 30)
    trigger_idx = n - _BARS_AFTER_TRIGGER - 1
    field = "high" if lhs == "bar.high" else "low"

    # Pick the spike direction by op + field so the LHS column actually
    # moves AWAY from the indicator's anchor (≈ base_close) in the cross
    # direction. The previous else-branch returned the wrong-direction spike
    # for (cross_below + bar.high) and (cross_above + bar.low):
    #   - cross_below requires LHS to drop UNDER the indicator → spike DOWN.
    #   - cross_above requires LHS to rise OVER the indicator → spike UP.
    # That's the same direction regardless of which field carries the LHS.
    if op == "cross_above":
        spike_value = base_close * 2.0
    else:
        spike_value = max(_MIN_PRICE, base_close * 0.5)

    return [
        lambda: _high_low_spike_bars(n, base_close, trigger_idx, field, spike_value),
        *_builders_for_priceref_cross(rhs, op, base_close, min_bars),
    ]


def _builders_for_volume_priceref_cross(
    rhs: IndicatorRef, op: str, base_close: float, min_bars: int
) -> List[Callable[[], List[OHLCVBar]]]:
    """Volume-varying forcing-sequence catalogue for ``bar.volume cross_* indicator``.

    The OHLC builders used by :func:`_builders_for_priceref_cross` hold volume
    flat at 1e6, so `bar.volume` (which evaluates to that same 1e6) never
    crosses a volume-source indicator (also ≈ 1e6) or a price-scale indicator
    (≈ base_close). Drive volume monotonically — or through a regime change
    so volume-RSI / volume-MACD escape the saturation trap on flat input —
    while holding close steady so the RHS indicator behaves predictably.

    Volume scale is chosen to match the RHS:

    * ``source="volume"`` (volume-side SMA/EMA/RSI/...): volume in volume
      magnitude (1e6); the indicator and the volume sit in the same range.
    * ``vwap`` and OHLC-source indicators: close is flat at ``base_close``
      so the indicator sits in price magnitude (~base_close). Volume must
      straddle ``base_close`` for the cross to be visible.
    """
    warmup = _warmup_for_indicator(rhs)
    n = max(min_bars, warmup + 60)
    direction = 1.0 if op == "cross_above" else -1.0

    if rhs.name != "vwap" and rhs.source == "volume":
        base_vol = 1_000_000.0
        slope = base_vol * 0.02
        signed_slope = direction * slope
        if direction > 0:
            primary_start, secondary_start = base_vol * 0.5, base_vol * 0.1
        else:
            primary_start, secondary_start = base_vol * 1.5, base_vol * 3.0
        return [
            lambda: _volume_trend_bars(n, base_close, primary_start, signed_slope),
            lambda: _volume_trend_bars(n, base_close, secondary_start, signed_slope),
            lambda: _volume_regime_change_bars(n, base_close, base_vol, slope),
            lambda: _volume_regime_change_bars(n, base_close, base_vol, slope, reverse=True),
        ]

    base_vol = float(base_close)
    slope = base_vol * 0.05
    signed_slope = direction * slope
    if direction > 0:
        primary_start, secondary_start = base_vol * 0.3, base_vol * 0.1
    else:
        primary_start, secondary_start = base_vol * 3.0, base_vol * 5.0
    return [
        lambda: _volume_trend_bars(n, base_close, primary_start, signed_slope),
        lambda: _volume_trend_bars(n, base_close, secondary_start, signed_slope),
        lambda: _volume_regime_change_bars(n, base_close, base_vol, slope),
        lambda: _volume_regime_change_bars(n, base_close, base_vol, slope, reverse=True),
    ]


def _builders_for_priceref_cross(
    rhs: IndicatorRef, op: str, base_close: float, min_bars: int
) -> List[Callable[[], List[OHLCVBar]]]:
    """Per-indicator forcing-sequence catalogue for ``priceref cross indicator``.

    Catalogues are ordered most-likely-to-fire first; the search returns on
    the first hit so later builders only run if earlier ones miss.
    """
    name = rhs.name
    if name == "bollinger":
        n = max(min_bars, int(rhs.param("period")) + 30)
        # Quiet history then breakout opens the bands wide; the close pierces
        # the upper / lower band on the spike side.
        direction_primary = "up" if op == "cross_above" else "down"
        direction_secondary = "down" if op == "cross_above" else "up"
        return [
            lambda: _quiet_then_breakout(n, base_close, direction_primary),
            lambda: _quiet_then_breakout(n, base_close, direction_secondary),
            lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
        ]
    if name == "rsi":
        period = int(rhs.param("period"))
        n = max(min_bars, period * 4 + 30)
        # RSI under pure monotonic drift saturates at 0 or 100. A regime
        # change is what actually sweeps it across an intermediate level.
        if op == "cross_above":
            return [
                lambda: _regime_change_bars(n, base_close, drop_rate=0.97, rise_rate=1.03),
                lambda: _regime_change_bars(
                    n, base_close, drop_rate=0.97, rise_rate=1.03, midpoint_frac=0.3
                ),
                lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
            ]
        return [
            lambda: _regime_change_bars(n, base_close, drop_rate=1.03, rise_rate=0.97),
            lambda: _regime_change_bars(
                n, base_close, drop_rate=1.03, rise_rate=0.97, midpoint_frac=0.3
            ),
            lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
        ]
    if name == "macd":
        slow = int(rhs.param("slow"))
        signal_ = int(rhs.param("signal"))
        n = max(min_bars, slow + signal_ + 60)
        # Slower decay/rise lets the histogram cross zero AFTER MACD warmup.
        # Aggressive 0.97/1.03 rates push the cross into warmup-NaN range.
        if op == "cross_above":
            return [
                lambda: _regime_change_bars(n, base_close, drop_rate=0.98, rise_rate=1.02),
                lambda: _regime_change_bars(n, base_close, drop_rate=0.99, rise_rate=1.005),
                lambda: _monotonic_trend_bars(n, base_close, 0.5),
            ]
        return [
            lambda: _regime_change_bars(n, base_close, drop_rate=1.02, rise_rate=0.98),
            lambda: _regime_change_bars(n, base_close, drop_rate=1.005, rise_rate=0.99),
            lambda: _monotonic_trend_bars(n, base_close, -0.5),
        ]
    if name == "adx":
        period = int(rhs.param("period"))
        n = max(min_bars, period * 4 + 60)
        # ADX needs a flat baseline followed by a sustained trend to climb
        # from low → high (for cross_above), or trend → quiet for cross_below.
        if op == "cross_above":
            return [
                lambda: _quiet_then_trend_bars(n, base_close, direction="up"),
                lambda: _quiet_then_trend_bars(n, base_close, direction="down"),
                lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
            ]
        return [
            lambda: _quiet_then_trend_bars(n, base_close, direction="up", trend_first=True),
            lambda: _quiet_then_trend_bars(n, base_close, direction="down", trend_first=True),
            lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
        ]
    if name == "atr":
        # ATR averages true range; the forcing sequence must widen
        # (cross_above) or tighten (cross_below) bar high-low ranges over
        # the ATR period. For ``priceref cross ATR`` the close must also
        # vary in the ATR magnitude — typical ATR is much smaller than
        # typical close (~5% of price), so the priceref builder also
        # ramps close.
        period = int(rhs.param("period"))
        n = max(min_bars, period * 4 + 30)
        amplitude = 10.0
        if op == "cross_above":
            # Range widens over time; for priceref-vs-ATR the close ramp
            # needs reverse=True so the close ends ABOVE the rising ATR.
            return [
                lambda: _widening_range_bars(n, base_close, amplitude),
                lambda: _widening_range_bars(n, base_close, amplitude * 2),
                lambda: _close_and_range_ramp_bars(n, amplitude, direction="up", reverse=True),
                lambda: _close_and_range_ramp_bars(n, amplitude, direction="down", reverse=True),
            ]
        # cross_below: ATR shrinks (reverse=True) or close drops below
        # falling ATR (direction without reverse).
        return [
            lambda: _widening_range_bars(n, base_close, amplitude, reverse=True),
            lambda: _widening_range_bars(n, base_close, amplitude * 2, reverse=True),
            lambda: _close_and_range_ramp_bars(n, amplitude, direction="up"),
            lambda: _close_and_range_ramp_bars(n, amplitude, direction="down"),
        ]
    if name == "vwap":
        # VWAP is a cumulative volume-weighted average — under any sustained
        # forcing the late-bar VWAP "lags" the close monotonically. The
        # compiled strategy doesn't evaluate VWAP until ``len(history) >=
        # _COMPILER_MIN_WINDOW`` (=20), so the cross must fire AFTER bar
        # 20. Step-regime is the only shape that reliably drives VWAP
        # across an arbitrary threshold inside the visible window: flat at
        # low_level long enough for VWAP to settle there, then flat at
        # high_level long enough to drag VWAP through the threshold.
        n = max(min_bars, _MIN_TOTAL_BARS * 2)
        if op == "cross_above":
            low_level = max(_MIN_PRICE, base_close * 0.5)
            high_level = base_close * 2.0
            return [
                lambda: _step_bars(n, low_level, high_level, step_frac=0.3),
                lambda: _step_bars(n, low_level, high_level, step_frac=0.5),
                lambda: _monotonic_trend_bars(n, base_close, base_close * 0.01),
            ]
        low_level = base_close * 2.0
        high_level = max(_MIN_PRICE, base_close * 0.5)
        return [
            lambda: _step_bars(n, low_level, high_level, step_frac=0.3),
            lambda: _step_bars(n, low_level, high_level, step_frac=0.5),
            lambda: _monotonic_trend_bars(n, base_close, -base_close * 0.01),
        ]
    if name == "stochastic":
        n = max(min_bars, _MIN_TOTAL_BARS * 2)
        if op == "cross_above":
            return [
                lambda: _regime_change_bars(n, base_close, drop_rate=0.97, rise_rate=1.03),
                lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
                lambda: _monotonic_trend_bars(n, base_close, 0.5),
            ]
        return [
            lambda: _regime_change_bars(n, base_close, drop_rate=1.03, rise_rate=0.97),
            lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
            lambda: _monotonic_trend_bars(n, base_close, -0.5),
        ]
    if name in ("sma", "ema"):
        n = max(min_bars, int(rhs.param("period")) + 30)
        if rhs.source == "volume":
            # Volume-source SMA/EMA — the indicator reads the volume column,
            # so close-varying builders keep SMA(volume) pinned at 1e6 and
            # bar.close (~base_close) never crosses it. Anchor volume in
            # price magnitude (~base_close) so SMA(volume) lives in the
            # same range as close, then ramp close through it.
            close_slope = 0.5 if op == "cross_above" else -0.5
            return [
                lambda: _close_ramp_with_priced_volume(n, base_close, close_slope),
                lambda: _close_ramp_with_priced_volume(n, base_close, close_slope * 2),
            ]
        # Fallback when lhs isn't bar.close (e.g. bar.high vs SMA). The SMA
        # is computed over closes (default source); a regime change makes
        # close drift below then above the lagged SMA so bar-derived fields
        # (high / low / open) cross too.
        if op == "cross_above":
            return [
                lambda: _regime_change_bars(n, base_close, drop_rate=0.97, rise_rate=1.03),
                lambda: _monotonic_trend_bars(n, base_close, 0.5),
            ]
        return [
            lambda: _regime_change_bars(n, base_close, drop_rate=1.03, rise_rate=0.97),
            lambda: _monotonic_trend_bars(n, base_close, -0.5),
        ]
    n = max(min_bars, _MIN_TOTAL_BARS)
    return [lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER)]


def _lookback_for_synth(ref: IndicatorRef) -> int:
    """Mirror ``synthesis/compiler.py:_lookback_for`` — minimum ``len(history)``
    before ``ref`` yields a non-``None`` value.

    Used to compute the runtime warmup floor (compiler gates ``on_bar`` at
    ``len(history) >= max(per_ref_lookback, _MIN_WINDOW)``) and the
    bounded history depth (``_history_depth_for_synth``).
    """
    name = ref.name
    if name in ("sma", "ema"):
        return int(ref.param("period"))
    if name == "rsi":
        return int(ref.param("period")) + 1
    if name == "macd":
        slow = int(ref.param("slow"))
        signal = int(ref.param("signal"))
        select = str(ref.param("output"))
        if select == "macd":
            return slow
        return slow + signal - 1
    if name == "bollinger":
        return int(ref.param("period"))
    if name == "atr":
        return int(ref.param("period")) + 1
    if name == "adx":
        return 2 * int(ref.param("period")) + 1
    if name == "stochastic":
        return int(ref.param("k_period")) + int(ref.param("d_period")) - 1
    if name == "vwap":
        return _COMPILER_MIN_WINDOW
    return 1


def _warmup_for_indicator(ref: IndicatorRef) -> int:
    """Minimum bar index at which ``ref`` has produced a finite value AND
    the compiled strategy would evaluate it (i.e. past the
    ``_COMPILER_MIN_WINDOW`` global gate).

    The cross search starts scanning from this index + 1 (it needs the
    previous bar too). Conservative — over-estimating warmup just delays the
    first match by a few bars, which is harmless.
    """
    return max(_lookback_for_synth(ref) + 1, _COMPILER_MIN_WINDOW)


def _builders_for_indicator_number(
    lhs: IndicatorRef, op: str, rhs: float, base_close: float, min_bars: int
) -> List[Callable[[], List[OHLCVBar]]]:
    """Threshold-aware forcing-sequence catalogue for ``indicator cross_* N``.

    The priceref-vs-indicator catalogue uses a fixed ``base_close`` (≈100)
    and slope (≈0.5), which produces indicator values in a fixed magnitude
    range — RSI ∈ [0, 100], MACD ∈ ~[-2, +2], Bollinger upper ∈ ~[base±10].
    That suffices for bounded oscillators against any in-range threshold, but
    fails for unbounded indicators (MACD, VWAP) and band indicators against
    thresholds outside the canned range (e.g. ``macd cross_above 50``,
    ``bollinger upper cross_below 100``). Scale the builders so the
    indicator's value range straddles ``rhs``.
    """
    name = lhs.name

    # source=volume — the indicator reads from the volume column, so
    # price-varying builders produce a flat indicator. Drive volume
    # monotonically with a flat warmup prefix (so the cross fires after
    # the indicator's warmup, not during it).
    if lhs.source == "volume":
        warmup = _warmup_for_indicator(lhs)
        n = max(min_bars, warmup + 60)
        base_vol = max(1.0, abs(rhs) if rhs != 0 else 1_000_000.0)
        direction = 1.0 if op == "cross_above" else -1.0
        # Slope floor scales with the absolute magnitudes involved; for
        # rhs=0 we still need a non-trivial slope so MACD differentiates.
        if name == "macd":
            slow = int(lhs.param("slow"))
            fast = int(lhs.param("fast"))
            spread_period = max(1, slow - fast)
            output = lhs.param("output")
            if output == "histogram":
                # Histogram transient peak ≈ slope, much smaller than
                # the line — needs a steeper slope to clear |rhs|.
                target_slope = max(base_vol * 0.01, 2.0 * abs(rhs))
            else:
                target_slope = max(base_vol * 0.01, 4.0 * abs(rhs) / spread_period)
        else:
            target_slope = max(base_vol * 0.01, abs(rhs) * 0.1)

        def _flat_then_volume(slope: float, start_vol: float) -> List[OHLCVBar]:
            flat = [
                OHLCVBar(
                    date="placeholder",
                    open=base_close,
                    high=base_close + 0.5,
                    low=max(_MIN_PRICE, base_close - 0.5),
                    close=base_close,
                    volume=start_vol,
                )
                for _ in range(warmup)
            ]
            trend = [
                OHLCVBar(
                    date="placeholder",
                    open=base_close,
                    high=base_close + 0.5,
                    low=max(_MIN_PRICE, base_close - 0.5),
                    close=base_close,
                    volume=max(1.0, start_vol + slope * (i + 1)),
                )
                for i in range(n - warmup)
            ]
            return flat + trend

        # For ANY cross we want the volume series to start on the opposite
        # side of ``rhs`` from the cross direction, then ramp THROUGH it:
        # cross_above ⇒ start below ``rhs``, slope up; cross_below ⇒ start
        # above, slope down. The earlier code paired `direction*slope` with
        # `-direction*slope` as if they were mirror builders, but for
        # cross_below the second variant started ABOVE the threshold and
        # then ramped further UP — never crossing back down.
        if direction > 0:
            primary_start = base_vol * 0.5
            secondary_start = base_vol * 0.1
        else:
            primary_start = base_vol * 1.5
            secondary_start = base_vol * 3.0
        signed_slope = direction * target_slope
        builders: List[Callable[[], List[OHLCVBar]]] = [
            lambda: _flat_then_volume(signed_slope, primary_start),
            lambda: _flat_then_volume(signed_slope, secondary_start),
            lambda: _volume_trend_bars(n, base_close, primary_start, signed_slope),
        ]
        # RSI saturates to 100 on flat input (no losses → RS = ∞) and to 0
        # on a pure rising / falling ramp once warmed in that direction —
        # a monotonic ramp never sweeps RSI through an interior threshold
        # from the opposite side. The volume regime-change builder drops
        # then rises (or rises then drops with ``reverse=True``), so RSI
        # crosses ``rhs`` on the second-half swing irrespective of the
        # saturation it parked at during the first half.
        if name == "rsi":
            regime_slope = max(base_vol * 0.05, target_slope)
            builders.extend(
                [
                    lambda: _volume_regime_change_bars(n, base_close, base_vol, regime_slope),
                    lambda: _volume_regime_change_bars(
                        n, base_close, base_vol, regime_slope, reverse=True
                    ),
                ]
            )
        if name == "macd":
            # MACD on flat input = 0; a monotonic volume ramp drifts MACD in
            # one direction (positive for rising volume, negative for
            # falling). That covers `cross_above N` for positive N reached
            # by a rising ramp, and `cross_below N` for negative N reached
            # by a falling ramp — but never the *opposite* side:
            #   - `cross_below N` for N > 0 needs MACD ≥ N first (a peak
            #     above N), then a fall back through N.
            #   - `cross_above N` for N < 0 needs MACD ≤ N first (a trough
            #     below N), then a rise back through N.
            #   - `cross_below N` for N < 0 needs MACD to actually visit
            #     below N (flat-warmup MACD = 0 ≥ N, so prev≥N is free;
            #     the monotonic falling ramp's trough may not reach N if
            #     |N| > peak |MACD| from that builder).
            #
            # ``_volume_peak_then_trough_bars`` drives volume up to
            # ``base_volume + amplitude`` then down to
            # ``base_volume - amplitude`` (or the reverse), producing a
            # MACD trajectory with BOTH a positive peak and a negative
            # trough. Sizing: peak |MACD| under EMA(fast)-EMA(slow) scales
            # with the volume swing; empirically peak |MACD line| ≈ 0.15 ×
            # amplitude for the default 12/26 EMAs over the warmup-bounded
            # window the search scans. The histogram (line − signal) and
            # the signal track at a smaller magnitude — peak |histogram| ≈
            # 0.03 × amplitude — so they need ~5× more amplitude than the
            # line to reach the same threshold. Overshoot |rhs| by a
            # generous factor so peaks comfortably clear thresholds across
            # the supported parameter range.
            output = lhs.param("output")
            magnitude_multiplier = 50.0 if output == "histogram" else 10.0
            peak_amplitude = max(base_vol, magnitude_multiplier * abs(rhs), 200.0)
            anchor = max(base_vol, peak_amplitude)
            builders.extend(
                [
                    lambda: _volume_peak_then_trough_bars(n, base_close, anchor, peak_amplitude),
                    lambda: _volume_peak_then_trough_bars(
                        n, base_close, anchor, peak_amplitude, reverse=True
                    ),
                ]
            )
        return builders

    if name in ("rsi", "adx", "stochastic"):
        # Bounded 0-100; the price-space builders already produce the full
        # range. Threshold scaling not needed.
        return _builders_for_priceref_cross(lhs, op, base_close, min_bars)
    if name == "atr":
        # ATR scales with high-low range, not close magnitude. Scale the
        # widening amplitude to overshoot ``rhs`` by 2× so the cross is
        # interior to the visible window rather than at the edge.
        period = int(lhs.param("period"))
        n = max(min_bars, period * 4 + 30)
        max_amp = max(2.0, abs(rhs) * 2.0)
        if op == "cross_above":
            return [
                lambda: _widening_range_bars(n, base_close, max_amp),
                lambda: _widening_range_bars(n, base_close, max_amp * 2),
            ]
        return [
            lambda: _widening_range_bars(n, base_close, max_amp, reverse=True),
            lambda: _widening_range_bars(n, base_close, max_amp * 2, reverse=True),
        ]
    if name == "vwap":
        anchored = max(_MIN_PRICE, abs(rhs) if rhs != 0 else base_close)
        return _builders_for_priceref_cross(lhs, op, anchored, min_bars)
    if name == "bollinger" and lhs.param("band") == "middle":
        # The middle band is just SMA(close, period). Delegate to the same
        # step-regime shape ``_synth_cross_indicator_number`` uses for SMA
        # so any in-price-range threshold works.
        period = int(lhs.param("period"))
        n = max(min_bars, period * 3 + 30)
        if op == "cross_above":
            low = rhs - 10.0
            high = rhs + max(20.0, abs(rhs))
        else:
            low = max(_MIN_PRICE, rhs - max(20.0, abs(rhs)))
            high = rhs + 10.0
            # For cross_below, step DOWN: start above the threshold, end below.
            low, high = high, low
        return [lambda: _step_bars(n, low, high, step_frac=0.5)]
    if name == "bollinger":
        # Bollinger band magnitude depends on both the rolling middle (close
        # SMA) and σ. Different cross shapes need different band dynamics:
        # quiet → breakout widens the band; high-vol → quiet collapses it.
        # The middle can be anchored above or below ``rhs`` to position the
        # cross in either direction. Try all four combinations — the search
        # returns on the first hit.
        period = int(lhs.param("period"))
        n = max(min_bars, period * 4 + 30)
        anchor_above = max(_MIN_PRICE, abs(rhs) + 5.0 if rhs > 0 else base_close + 5.0)
        anchor_below = max(_MIN_PRICE, abs(rhs) - 5.0 if rhs > 0 else base_close - 5.0)
        return [
            # Anchor near the threshold from below + widen the band.
            lambda: _quiet_then_breakout(n, anchor_below, "up"),
            lambda: _quiet_then_breakout(n, anchor_below, "down"),
            # Anchor above + collapse the band.
            lambda: _high_vol_then_quiet(n, anchor_above, vol_frac=0.6, amplitude=8.0),
            lambda: _high_vol_then_quiet(n, anchor_below, vol_frac=0.6, amplitude=8.0),
            # Larger amplitudes for further-out thresholds.
            lambda: _high_vol_then_quiet(n, anchor_above, vol_frac=0.4, amplitude=12.0),
            lambda: _quiet_then_breakout(n, anchor_above, "up"),
            lambda: _quiet_then_breakout(n, anchor_above, "down"),
        ]
    if name == "macd":
        # MACD line / signal / histogram magnitude scales with the EMA
        # spread, which scales with slope × (slow - fast) / 2 at steady
        # state. To make MACD cross ``rhs`` strictly (not asymptote to it),
        # overshoot by a factor of 4 — steady-state MACD ≈ 2|rhs|.
        #
        # Critical: prefix the trending phase with a flat segment longer
        # than the MACD warmup so MACD enters the visible window at zero
        # and the cross fires *after* warmup, not during it. Without the
        # prefix, low-magnitude thresholds (|rhs| ≤ ~5) are crossed during
        # warmup and the search sees only post-cross values.
        slow = int(lhs.param("slow"))
        fast = int(lhs.param("fast"))
        signal_ = int(lhs.param("signal"))
        warmup = slow + signal_ + 5
        n = max(min_bars, warmup + 90)
        spread_period = max(1, slow - fast)
        # Steady-state MACD line ≈ slope × spread_period / 2; histogram's
        # transient peak ≈ slope (much smaller than the line). Use the
        # output-specific formula so we don't overshoot — overshooting
        # forces base_close to clamp at MIN_PRICE in the two-phase builder,
        # which kills the negative MACD seeding signed-threshold crosses
        # depend on.
        output = lhs.param("output")
        if output == "histogram":
            target_slope = max(0.5, 2.0 * abs(rhs))
        else:
            # line / signal both scale with the EMA spread.
            target_slope = max(0.5, 4.0 * abs(rhs) / spread_period)
        direction = 1.0 if op == "cross_above" else -1.0

        # Lift base_close so the steep target_slope doesn't clamp the
        # trend at _MIN_PRICE. Histogram's transient peak forms over
        # ~signal_period bars; if the close clamps before then, the
        # MACD line stops differentiating from signal and the histogram
        # transient (which is what carries large-magnitude crossings)
        # never reaches |rhs|.
        flat_then_trend_base = max(base_close, target_slope * (n - warmup) + 100.0)

        def _flat_then_trend(slope: float) -> List[OHLCVBar]:
            flat = [flat_then_trend_base] * warmup
            trend = [
                max(_MIN_PRICE, flat_then_trend_base + slope * (i + 1)) for i in range(n - warmup)
            ]
            closes = flat + trend
            return [
                OHLCVBar(
                    date="placeholder",
                    open=c,
                    high=c + 0.5,
                    low=max(_MIN_PRICE, c - 0.5),
                    close=c,
                    volume=1_000_000.0,
                )
                for c in closes
            ]

        # Two-phase trend for signed thresholds: when ``rhs`` sits on the
        # opposite side of zero from ``direction``, a single-phase trend
        # can't seed MACD on the far side of ``rhs``. E.g. ``cross_above -50``
        # needs MACD < -50 before rising through it; a flat warmup pins it
        # at 0 (already above -50). Phase 1 pushes MACD past ``rhs`` in
        # the opposite direction; phase 2 brings it back through ``rhs``.
        #
        # Phase 1 must be longer than the MACD warmup so MACD enters the
        # visible window already converged to the phase-1 steady state;
        # otherwise phase 1's MACD is NaN throughout and phase 2 starts
        # with MACD = 0 instead of the desired far-side value.
        two_phase_phase1_bars = warmup + 25
        two_phase_n = two_phase_phase1_bars + warmup + 30
        two_phase_phase1_frac = two_phase_phase1_bars / two_phase_n
        two_phase_base = max(base_close, target_slope * two_phase_phase1_bars + 100.0)
        phase1_slope = -direction * target_slope
        phase2_slope = direction * target_slope
        return [
            lambda: _flat_then_trend(direction * target_slope),
            lambda: _flat_then_trend(-direction * target_slope),
            lambda: _two_phase_trend_bars(
                two_phase_n,
                two_phase_base,
                phase1_slope,
                phase2_slope,
                phase1_frac=two_phase_phase1_frac,
            ),
            lambda: _monotonic_trend_bars(n, base_close, direction * target_slope),
            lambda: _regime_change_bars(n, base_close, drop_rate=0.98, rise_rate=1.02),
            lambda: _regime_change_bars(n, base_close, drop_rate=1.02, rise_rate=0.98),
        ]
    # Unknown indicator: fall through to the priceref catalogue.
    return _builders_for_priceref_cross(lhs, op, base_close, min_bars)


def _synth_cross_indicator_indicator(
    lhs: IndicatorRef, op: str, rhs: IndicatorRef, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Indicator-vs-indicator cross. Tries multiple forcing sequences via
    :func:`_search_cross`; finds the first bar where both indicators
    transition across each other.
    """
    longest_period = max(_warmup_for_indicator(lhs), _warmup_for_indicator(rhs))
    n = max(min_bars, longest_period * 4 + 30)
    warmup = longest_period

    # Volume-source pair: both indicators read the volume column. The
    # default OHLC-varying builders leave volume pinned to 1_000_000.0,
    # so both series stay flat. Drive volume monotonically with
    # different polarities so the shorter-period indicator crosses the
    # longer-period one (mirror of the close regime-change shape).
    #
    # Anchor selection: when BOTH sides are volume-source, anchor volume
    # at the realistic 1e6 scale. When ONLY ONE side is volume, the
    # other side reads close — anchoring volume at 1e6 keeps the two
    # series orders of magnitude apart and the cross can never fire.
    # Drop the anchor to ``base_close`` so the close-side indicator and
    # the volume-side indicator live in the same value range.
    if lhs.source == "volume" or rhs.source == "volume":
        both_volume = lhs.source == "volume" and rhs.source == "volume"
        base_vol = 1_000_000.0 if both_volume else float(base_close)
        volume_slope = base_vol * 0.02
        builders: List[Callable[[], List[OHLCVBar]]] = [
            lambda: _volume_regime_change_bars(n, base_close, base_vol, volume_slope),
            lambda: _volume_regime_change_bars(n, base_close, base_vol, volume_slope, reverse=True),
            lambda: _volume_trend_bars(n, base_close, base_vol * 0.5, volume_slope),
            lambda: _volume_trend_bars(n, base_close, base_vol * 1.5, -volume_slope),
        ]
        bars, trigger, reason = _search_cross(
            lhs, op, rhs, base_close, min_bars, builders, warmup=warmup
        )
        if bars is not None:
            return bars, trigger, None
        return None, 0, reason

    builders = [
        lambda: _regime_change_bars(n, base_close),
        lambda: _regime_change_bars(n, base_close, drop_rate=1.02, rise_rate=0.97),
        lambda: _regime_change_bars(n, base_close, midpoint_frac=0.3),
        lambda: _regime_change_bars(n, base_close, midpoint_frac=0.7),
        lambda: _monotonic_trend_bars(n, base_close, 0.5),
        lambda: _monotonic_trend_bars(n, base_close, -0.5),
        lambda: _high_volatility_bars(n, base_close, _BARS_AFTER_TRIGGER),
    ]
    bars, trigger, reason = _search_cross(
        lhs, op, rhs, base_close, min_bars, builders, warmup=warmup
    )
    if bars is not None:
        return bars, trigger, None
    return None, 0, reason or "indicator_indicator_cross_not_found_in_window"


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
    # SMA/EMA on any close-derived source (close/high/low/open/hl2/ohlc4):
    # use the deterministic step-regime shape. The source columns all
    # track close in lockstep (high = close + 0.5, low = close - 0.5,
    # open = close, hl2 = close, ohlc4 = close + 0.125), so a step in
    # close sweeps SMA(<source>) through the threshold the same way as
    # SMA(close). The fast-path is anchored to ``rhs`` rather than
    # ``base_close``, so it works for arbitrarily-positioned thresholds.
    # Volume source falls through to the source-aware catalogue below
    # since the volume column doesn't track close.
    if lhs.name in ("sma", "ema") and lhs.source != "volume":
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
        source_series = _series_for_source(df, lhs.source)
        ma_series = (sma if lhs.name == "sma" else ema)(source_series, period)
        for idx in range(period + 1, n):
            if _verify_cross(ma_series.iloc[idx - 1], ma_series.iloc[idx], rhs, rhs, op):
                return bars, idx, None
        return None, 0, "indicator_number_cross_not_found"

    # Non-MA indicators (RSI, MACD, ADX, Stochastic, Bollinger, VWAP) cross a
    # numeric threshold. Scale the forcing-sequence magnitude by ``rhs`` so
    # arbitrary thresholds reach the cross window (a fixed ``base_close``
    # catalogue only covers thresholds the canned price paths happen to
    # straddle — e.g. ``macd cross_above 50`` needs a much steeper slope
    # than ``macd cross_above 0``).
    builders = _builders_for_indicator_number(lhs, op, rhs, base_close, min_bars)
    warmup = _warmup_for_indicator(lhs)
    return _search_cross(lhs, op, rhs, base_close, min_bars, builders, warmup=warmup)


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
    computable and the inequality holds at the trigger bar.

    Drives the series corresponding to ``lhs.source`` (which must equal
    ``rhs.source`` — mismatched sources are semantically odd and rejected
    explicitly rather than producing a misleading trigger). For
    ``source="volume"`` varies the volume column; for any close-derived
    source varies close. Indicator computation goes through the same
    source-aware path the engine uses, so a fast/slow asymmetry is
    handled correctly: with close trending up at slope +0.5, the
    shorter-period MA leads the longer; so ``slope_sign`` is chosen to
    make the requested inequality hold at the right edge, irrespective
    of which side is the faster MA.
    """
    if {lhs.name, rhs.name} - {"sma", "ema"}:
        return None, 0, f"indicator_vs_indicator_unsupported:{lhs.name}_{rhs.name}"
    if lhs.source != rhs.source:
        return None, 0, (f"indicator_vs_indicator_mismatched_source:{lhs.source}_{rhs.source}")
    source = lhs.source
    lhs_period = int(lhs.param("period"))
    rhs_period = int(rhs.param("period"))
    longest = max(lhs_period, rhs_period)
    n = max(min_bars, longest + 30)

    # Reject same-name same-period pairs — both indicators produce
    # identical values on every bar, so no strict inequality can hold
    # and no satisfying trigger exists.
    if lhs.name == rhs.name and lhs_period == rhs_period and op in (">", "<"):
        return None, 0, "indicator_vs_indicator_identical_sides"

    # For monotonic trends the FASTER MA leads — its value is closer to
    # the current bar. So lhs_is_faster (lhs leads rhs) implies that, on
    # a rising trend, lhs > rhs; on a falling trend, lhs < rhs. Pick the
    # slope sign that makes the requested `lhs op rhs` hold.
    #
    # Effective period orders MA types: at the same period, EMA reacts
    # faster than SMA (weight on recent values; half-life ≈ N/2). So
    # `EMA(10) > SMA(10)` holds on a *rising* trend, while
    # `SMA(10) > EMA(10)` holds on a *falling* trend — the same period
    # alone doesn't determine ordering, the indicator name does. The
    # earlier code only consulted period and reported these same-period
    # cases as unprobeable in one direction.
    def _effective_period(name: str, period: int) -> float:
        if name == "ema":
            return period * 0.5
        return float(period)

    lhs_eff = _effective_period(lhs.name, lhs_period)
    rhs_eff = _effective_period(rhs.name, rhs_period)
    want_lhs_greater = op in (">", ">=")
    lhs_is_faster = lhs_eff < rhs_eff
    slope_sign = 1.0 if (want_lhs_greater == lhs_is_faster) else -1.0

    if source == "volume":
        base_vol = 1_000_000.0
        volume_slope = base_vol * 0.02 * slope_sign
        bars = [
            OHLCVBar(
                date="placeholder",
                open=base_close,
                high=base_close + 0.5,
                low=max(_MIN_PRICE, base_close - 0.5),
                close=base_close,
                volume=max(1.0, base_vol + volume_slope * i),
            )
            for i in range(n)
        ]
    else:
        close_slope = 0.5 * slope_sign
        closes = [max(_MIN_PRICE, base_close + i * close_slope) for i in range(n)]
        bars = [
            OHLCVBar(
                date="placeholder",
                open=c,
                high=c + 0.5,
                low=max(_MIN_PRICE, c - 0.5),
                close=c,
                volume=1_000_000.0,
            )
            for c in closes
        ]
    df = _bars_to_df(bars)
    source_series = _series_for_source(df, source)
    lhs_series = (sma if lhs.name == "sma" else ema)(source_series, lhs_period)
    rhs_series = (sma if rhs.name == "sma" else ema)(source_series, rhs_period)
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


def _synth_indicator_vs_bar_volume(
    lhs: IndicatorRef, op: str, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """`SMA/EMA op bar.volume`: drive the bar's volume column so the MA's
    value sits on the correct side of the current bar's volume at the
    trigger.

    For close-source MA: the MA value equals ``base_close`` (close is held
    flat); set the trigger bar's volume below or above base_close so the
    inequality with the MA value holds.

    For volume-source MA: trend the volume column so the MA lags the
    current bar's volume — falling volume → MA averages older higher
    volumes → MA > current; rising volume → MA averages older lower
    volumes → MA < current.

    Volumes are anchored in price magnitude (around base_close) so the
    MA value and bar.volume are directly comparable. The trigger bar's
    volume is the divergence point: at warmup-1, the MA has just become
    valid and the inequality is tightest, giving a stable trigger index.
    """
    if lhs.name not in ("sma", "ema"):
        return None, 0, f"indicator_vs_priceref_unsupported:{lhs.name}_bar.volume"

    period = int(lhs.param("period"))
    n = max(min_bars, _required_bars_for_indicator(lhs))
    trigger_idx = max(period, n - _BARS_AFTER_TRIGGER - 1)

    want_ma_greater = op in (">", ">=")

    if lhs.source == "volume":
        # Trend volume in price magnitude. Anchor large enough that the
        # ramp doesn't hit _MIN_PRICE inside the visible window.
        anchor = max(base_close * 2.0, float(n) + 50.0)
        vol_slope = -1.0 if want_ma_greater else 1.0
        midpoint = n // 2
        volumes = [max(_MIN_PRICE, anchor + (i - midpoint) * vol_slope) for i in range(n)]
    else:
        # Close-source MA: MA value ≈ base_close. Set trigger bar's volume
        # to the opposite side of base_close. All other bars keep volume
        # at base_close so the MA-vs-volume inequality only holds at the
        # trigger — deterministic single-bar trigger.
        target_vol = base_close * 0.1 if want_ma_greater else base_close * 10.0
        volumes = [float(base_close)] * n
        volumes[trigger_idx] = max(_MIN_PRICE, target_vol)

    bars = [
        OHLCVBar(
            date="placeholder",
            open=base_close,
            high=base_close + 0.5,
            low=max(_MIN_PRICE, base_close - 0.5),
            close=base_close,
            volume=v,
        )
        for v in volumes
    ]
    df = _bars_to_df(bars)
    source_series = _series_for_source(df, lhs.source)
    ma_series = (sma if lhs.name == "sma" else ema)(source_series, period)
    max_trigger = max(period, n - _BARS_AFTER_TRIGGER - 1)
    for tidx in range(period, max_trigger + 1):
        lv = ma_series.iloc[tidx]
        if not (isinstance(lv, float) and math.isfinite(lv)):
            continue
        rv = bars[tidx].volume
        if _compare(float(lv), op, float(rv)):
            return bars, tidx, None
    return None, 0, f"indicator_vs_priceref_unsupported:{lhs.name}_bar.volume"


def _synth_indicator_vs_priceref(
    lhs: IndicatorRef, op: str, rhs: str, base_close: float, min_bars: int
) -> Tuple[Optional[List[OHLCVBar]], int, Optional[str]]:
    """Indicator-vs-PriceRef: drive the indicator value relative to the bar's
    price field by trending the column corresponding to ``lhs.source`` so
    SMA/EMA lags the current bar.

    The previous flat-bars + single-trigger-bar approach used
    ``max(bar.high, target)`` to clamp high — which silently did nothing
    when ``target < bar.high`` (the ``op in (">", ">=")`` case), so
    ``SMA > bar.high`` was always returned as unprobeable. Trending the
    source column avoids that clamp bug entirely.

    For source=close/high/low/open: trend close; high/low/open track close.
    For source=volume: trend the volume column in price magnitude so the
    volume-source MA lives in the same range as bar.<priceref> and the
    inequality can actually hold; the runtime's source-aware
    ``_compute_indicator_at`` agrees because it also reads volume.

    Volume-rhs (`MA op bar.volume`) is handled by a separate branch — the
    synthesizer controls the volume column and can drive it independently
    of the MA (close-source MA stays anchored at ``base_close`` while
    bar.volume jumps to a target; volume-source MA lags a trending volume
    so it sits above/below the current bar's volume).
    """
    if lhs.name not in ("sma", "ema"):
        return None, 0, f"indicator_vs_priceref_unsupported:{lhs.name}_{rhs}"
    rhs_field = rhs.split(".", 1)[1] if rhs.startswith("bar.") else None
    if rhs_field is None:
        return None, 0, f"indicator_vs_priceref_unsupported:{lhs.name}_{rhs}"
    if rhs_field == "volume":
        return _synth_indicator_vs_bar_volume(lhs, op, base_close, min_bars)

    period = int(lhs.param("period"))
    n = max(min_bars, _required_bars_for_indicator(lhs))

    # For `MA > bar.<field>` we want the MA above the current bar — so the
    # MA must average OLDER higher source values, i.e. the source is
    # trending DOWN now. For `MA < bar.<field>` mirror — source trending UP.
    source_slope = -0.5 if op in (">", ">=") else 0.5

    if lhs.source == "volume":
        # Trend the volume column in price magnitude so SMA(volume) and
        # bar.<priceref> live in the same range. Close stays at
        # ``base_close`` (bar.<priceref> = base_close on every bar), and
        # volume straddles base_close so SMA(volume) starts on one side
        # of bar.<priceref> and sweeps through. The midpoint offset is
        # what makes the inequality hold at the early-warmup trigger:
        # for op="<" volume ramps UP through base_close so early SMAs
        # (averaging values below base_close) satisfy MA < bar.close;
        # for op=">" volume ramps DOWN through base_close so early SMAs
        # sit above base_close.
        midpoint = n // 2
        volumes = [max(_MIN_PRICE, base_close + (i - midpoint) * source_slope) for i in range(n)]
        bars = [
            OHLCVBar(
                date="placeholder",
                open=base_close,
                high=base_close + 0.5,
                low=max(_MIN_PRICE, base_close - 0.5),
                close=base_close,
                volume=v,
            )
            for v in volumes
        ]
    else:
        closes = [max(_MIN_PRICE, base_close + i * source_slope) for i in range(n)]
        bars = [
            OHLCVBar(
                date="placeholder",
                open=c,
                high=c + 0.5,
                low=max(_MIN_PRICE, c - 0.5),
                close=c,
                volume=1_000_000.0,
            )
            for c in closes
        ]
    df = _bars_to_df(bars)
    source_series = _series_for_source(df, lhs.source)
    ma_series = (sma if lhs.name == "sma" else ema)(source_series, period)
    # Walk forward from the first valid MA bar; pick the earliest trigger
    # that leaves at least `_BARS_AFTER_TRIGGER` bars for engine evaluation.
    max_trigger = max(period, n - _BARS_AFTER_TRIGGER - 1)
    for trigger_idx in range(period, max_trigger + 1):
        lv = ma_series.iloc[trigger_idx]
        if not (isinstance(lv, float) and math.isfinite(lv)):
            continue
        rv = getattr(bars[trigger_idx], rhs_field)
        if _compare(float(lv), op, float(rv)):
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
        # Exact equality mirrors the compiler's raw ``lhs == rhs`` emission
        # (see ``strategy_lab/synthesis/compiler.py``). Using ``math.isclose``
        # here would make the probe accept near-equal floats that the
        # compiled strategy wouldn't — a recipe-vs-runtime mismatch that
        # surfaces as a false critical for any ``op == "=="`` predicate.
        return lhs == rhs
    return False
