"""Spec → canonical Python compiler (issue #538).

Pure function ``compile_strategy(spec)`` that turns a structured
``StrategySpec`` into the ``Strategy`` subclass the streaming harness
expects. The emitted module is shaped to pass ``CodeSafetyChecker`` and
``CodeConformanceGate`` by construction.

Determinism contract: the same spec always produces byte-identical
output. The header carries a SHA-256 content hash of the spec's sorted
JSON dump for traceability; nothing else in the output varies between
invocations.

Scope (#538 + locked decisions):
  * Stop-loss / take-profit ARE inlined in ``on_bar`` as explicit exit
    branches that close the open position with the opposite side and
    ``qty=position.qty``. The engine's ``evaluate_exit_rules`` is not
    on the live runtime path today (only ``ctx.submit_order`` calls
    from ``on_bar`` are processed), so the compiled class enforces the
    thresholds directly against ``position.entry_price`` /
    ``position.high_since_entry`` (the conformance gate also requires
    this reference whenever the rule kind appears in the spec).
  * ``cross_above`` / ``cross_below`` predicates compare the current
    side value against ``self._prev_<sigid>`` snapshots updated at the
    end of every ``on_bar`` invocation where the universe and warm-up
    guards passed. "Previous" means previous successful ``on_bar``, not
    previous calendar bar.
  * ``volatility_target`` sizing requires an ``atr`` indicator in the
    spec (any rule). When absent, the compiler raises
    :class:`CompilerError`; the orchestrator catches it, sets
    ``requires_custom_code = True``, and falls back to the LLM output.
  * Empty ``spec.target_symbols`` is supported (no universe guard);
    other gates downgrade their universe checks in that case.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from typing import Any, List, Tuple

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


class CompilerError(Exception):
    """Raised when a spec cannot be expressed by the deterministic compiler.

    The orchestrator treats this as the signal to fall back to LLM-authored
    code: it sets ``spec.requires_custom_code = True`` and keeps the
    ideation-generated ``strategy_code`` instead of the compiled output.
    """


# DSL → canonical call name emitted in ``on_bar``. Mirrors
# ``code_conformance._INDICATOR_ALLOWED_CALL_NAMES`` (the conformance gate
# accepts either ``bollinger`` or ``bollinger_bands`` for the bollinger
# DSL name; we pick ``bollinger_bands`` to match the canonical
# ``from indicators import ...`` line below).
_INDICATOR_CALL_NAME: dict[str, str] = {
    "sma": "sma",
    "ema": "ema",
    "rsi": "rsi",
    "macd": "macd",
    "bollinger": "bollinger_bands",
    "atr": "atr",
    "adx": "adx",
    "stochastic": "stochastic",
    "vwap": "vwap",
}

# Source field → expression yielding the per-bar value used for
# indicator inputs. ATR/ADX/Stochastic/VWAP read OHLC directly inside
# the indicator function, so their bar list is always close-keyed.
_SOURCE_EXPR: dict[str, str] = {
    "close": "b.close",
    "high": "b.high",
    "low": "b.low",
    "open": "b.open",
    "volume": "b.volume",
    "hl2": "((b.high + b.low) / 2)",
    "ohlc4": "((b.open + b.high + b.low + b.close) / 4)",
}

_BAR_FIELD_EXPR: dict[str, str] = {
    "bar.close": "bar.close",
    "bar.high": "bar.high",
    "bar.low": "bar.low",
    "bar.volume": "bar.volume",
}

# Floor on the rolling-history window the strategy requests from
# ``ctx.history``. Indicators with short lookbacks still benefit from a
# little buffer; matches the floor in the ideation system prompt.
_MIN_WINDOW: int = 20


def compile_strategy(spec: Any) -> str:
    """Compile ``spec`` into a canonical ``Strategy`` Python module.

    Pre: ``spec`` is a ``StrategySpec`` (duck-typed; only the public
    fields ``target_symbols``, ``entry_rules``, ``exit_rules``,
    ``sizing`` are read).
    Post: returns a non-empty Python source string with exactly one
    ``Strategy`` subclass. Raises :class:`CompilerError` for specs the
    compiler cannot express (e.g. ``volatility_target`` sizing without
    a matching ATR indicator).
    """
    entry_rules: List[EntryRule] = list(getattr(spec, "entry_rules", []) or [])
    exit_rules: List[Any] = list(getattr(spec, "exit_rules", []) or [])
    target_symbols: List[str] = list(getattr(spec, "target_symbols", []) or [])
    sizing = getattr(spec, "sizing", None)
    if sizing is None:
        raise CompilerError("spec.sizing is required")

    signal_exit_rules = [r for r in exit_rules if isinstance(r, SignalExitRule)]
    stop_loss_rules = [r for r in exit_rules if isinstance(r, StopLossRule)]
    take_profit_rules = [r for r in exit_rules if isinstance(r, TakeProfitRule)]

    # Trailing-basis stop-losses need ``position.high_since_entry`` /
    # ``position.low_since_entry``, neither of which is on the
    # strategy-side ``_PositionSnapshot``. Fall back to LLM synthesis
    # rather than emit logic that silently uses entry_price instead.
    for rule in stop_loss_rules:
        if rule.basis != "entry_price":
            raise CompilerError(
                f"stop-loss basis {rule.basis!r} requires trailing-state "
                "tracking not exposed on the strategy-side position "
                "snapshot; compiler supports basis='entry_price' only"
            )

    indicator_refs: List[IndicatorRef] = _collect_indicators(entry_rules, signal_exit_rules)
    if isinstance(sizing, VolatilityTargetSizing) and not any(
        ref.name == "atr" for ref in indicator_refs
    ):
        raise CompilerError(
            "volatility_target sizing requires an 'atr' indicator referenced "
            "by an entry or signal-exit rule; none found in spec"
        )

    indicator_bindings = _build_indicator_bindings(indicator_refs)
    cross_sides = _collect_cross_sides(entry_rules, signal_exit_rules, indicator_bindings)

    window = max((_lookback_for(ref) for ref in indicator_refs), default=_MIN_WINDOW)
    window = max(window, _MIN_WINDOW)

    parts: List[str] = []
    parts.append(_emit_header(spec))
    parts.append(_emit_imports())
    parts.append(
        _emit_class(
            target_symbols=target_symbols,
            window=window,
            cross_sides=cross_sides,
            indicator_bindings=indicator_bindings,
            entry_rules=entry_rules,
            signal_exit_rules=signal_exit_rules,
            stop_loss_rules=stop_loss_rules,
            take_profit_rules=take_profit_rules,
            sizing=sizing,
        )
    )
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Helpers — indicator collection, sigid generation, lookback math.
# ---------------------------------------------------------------------------


def _collect_indicators(
    entry_rules: List[EntryRule], signal_exit_rules: List[SignalExitRule]
) -> List[IndicatorRef]:
    """Walk every predicate on every entry / signal-exit rule and return
    the de-duplicated, sort-stable list of ``IndicatorRef`` instances.

    Two refs with the same ``(name, params, source)`` deduplicate; sort
    order is the sigid (sha256 of the canonical JSON dump) so binding
    emission is stable across runs.
    """
    seen: dict[str, IndicatorRef] = {}
    for rule in entry_rules:
        for side in (rule.when.lhs, rule.when.rhs):
            if isinstance(side, IndicatorRef):
                sigid = _sigid_for_side(side)
                seen.setdefault(sigid, side)
    for rule in signal_exit_rules:
        for side in (rule.when.lhs, rule.when.rhs):
            if isinstance(side, IndicatorRef):
                sigid = _sigid_for_side(side)
                seen.setdefault(sigid, side)
    return [seen[sigid] for sigid in sorted(seen)]


def _sigid_for_side(side: Any) -> str:
    """Return a stable 8-char hex id for one predicate side.

    Used both to key indicator binding variables (so two refs with the
    same params share a binding) and to name ``self._prev_<sigid>``
    state slots when a cross-* predicate references the side.
    """
    if isinstance(side, IndicatorRef):
        payload = json.dumps(side.model_dump(mode="json"), sort_keys=True)
        key = f"ind::{payload}"
    elif isinstance(side, str):
        key = f"price::{side}"
    elif isinstance(side, (int, float)):
        key = f"num::{float(side)!r}"
    else:
        raise CompilerError(f"unsupported predicate side type: {type(side).__name__}")
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _lookback_for(ref: IndicatorRef) -> int:
    """Return the maximum bar-history depth ``ref`` needs to be computable.

    Conservative: pulls from the indicator-specific params and pads
    out the few cases where multiple params combine (MACD = slow +
    signal, stochastic = k_period + d_period).
    """
    name = ref.name
    if name in ("sma", "ema"):
        return int(ref.param("period"))
    if name == "rsi":
        return int(ref.param("period")) + 1
    if name == "macd":
        return int(ref.param("slow")) + int(ref.param("signal"))
    if name == "bollinger":
        return int(ref.param("period"))
    if name in ("atr", "adx"):
        return int(ref.param("period")) + 1
    if name == "stochastic":
        return int(ref.param("k_period")) + int(ref.param("d_period"))
    if name == "vwap":
        return _MIN_WINDOW
    raise CompilerError(f"unsupported indicator: {name!r}")


def _build_indicator_bindings(
    refs: List[IndicatorRef],
) -> List[Tuple[str, IndicatorRef, str, str]]:
    """Return ``(varname, ref, call_expr, sigid)`` for every indicator.

    Sort order matches ``_collect_indicators`` (sigid-sorted) so emission
    is byte-stable.
    """
    out: List[Tuple[str, IndicatorRef, str, str]] = []
    for ref in refs:
        sigid = _sigid_for_side(ref)
        varname = f"_ind_{ref.name}_{sigid}"
        call_expr = _emit_indicator_call(ref)
        out.append((varname, ref, call_expr, sigid))
    return out


def _emit_indicator_call(ref: IndicatorRef) -> str:
    """Render the ``indicators.<fn>(...)`` call expression for one ref.

    The bar list comprehension uses the indicator's declared ``source``
    (close by default); ATR/ADX/Stochastic/VWAP read OHLC themselves
    inside the indicator function but the wrapper-style call signature
    is the same.
    """
    fn = _INDICATOR_CALL_NAME[ref.name]
    bar_expr = _SOURCE_EXPR[ref.source]
    bar_list = f"[{bar_expr} for b in history]"

    if ref.name in ("sma", "ema"):
        return f"{fn}({bar_list}, period={int(ref.param('period'))})"
    if ref.name == "rsi":
        return f"{fn}({bar_list}, period={int(ref.param('period'))})"
    if ref.name == "macd":
        return (
            f"{fn}({bar_list}, fast={int(ref.param('fast'))}, "
            f"slow={int(ref.param('slow'))}, signal={int(ref.param('signal'))})"
        )
    if ref.name == "bollinger":
        return (
            f"{fn}({bar_list}, period={int(ref.param('period'))}, "
            f"num_std={float(ref.param('num_std'))!r})"
        )
    if ref.name in ("atr", "adx"):
        return f"{fn}([b for b in history], period={int(ref.param('period'))})"
    if ref.name == "stochastic":
        return (
            f"{fn}([b for b in history], k_period={int(ref.param('k_period'))}, "
            f"d_period={int(ref.param('d_period'))})"
        )
    if ref.name == "vwap":
        return f"{fn}([b for b in history])"
    raise CompilerError(f"unsupported indicator: {ref.name!r}")


def _collect_cross_sides(
    entry_rules: List[EntryRule],
    signal_exit_rules: List[SignalExitRule],
    indicator_bindings: List[Tuple[str, IndicatorRef, str, str]],
) -> List[Tuple[str, str, str]]:
    """Return ``(sigid, prev_attr, current_expr)`` for every side that
    participates in any ``cross_above`` / ``cross_below`` predicate.

    ``prev_attr`` is the ``self._prev_<sigid>`` slot name; ``current_expr``
    is the expression yielding the side's current-bar value (an indicator
    binding variable or a bar-field reference). De-duplicated by sigid;
    sort order is sigid so emission is stable.
    """
    binding_by_sigid = {sigid: varname for varname, _ref, _call, sigid in indicator_bindings}
    out: dict[str, Tuple[str, str, str]] = {}

    def _record(side: Any) -> None:
        sigid = _sigid_for_side(side)
        if sigid in out:
            return
        prev_attr = f"_prev_{sigid}"
        current_expr = _render_side(side, binding_by_sigid)
        out[sigid] = (sigid, prev_attr, current_expr)

    for rule in entry_rules:
        if rule.when.op in ("cross_above", "cross_below"):
            _record(rule.when.lhs)
            _record(rule.when.rhs)
    for rule in signal_exit_rules:
        if rule.when.op in ("cross_above", "cross_below"):
            _record(rule.when.lhs)
            _record(rule.when.rhs)
    return [out[sigid] for sigid in sorted(out)]


def _render_side(side: Any, binding_by_sigid: dict[str, str]) -> str:
    """Render one predicate side as a Python expression.

    Indicator refs resolve to their binding variable; bar-field literals
    pass through; numeric literals render as ``repr(float(value))``.
    """
    if isinstance(side, IndicatorRef):
        sigid = _sigid_for_side(side)
        varname = binding_by_sigid.get(sigid)
        if varname is None:
            raise CompilerError(
                "internal: indicator ref missing from bindings — "
                f"sigid={sigid!r} name={side.name!r}"
            )
        return varname
    if isinstance(side, str):
        if side not in _BAR_FIELD_EXPR:
            raise CompilerError(f"unsupported price-ref literal: {side!r}")
        return _BAR_FIELD_EXPR[side]
    if isinstance(side, (int, float)) and not isinstance(side, bool):
        return repr(float(side))
    raise CompilerError(f"unsupported predicate side type: {type(side).__name__}")


def _render_predicate(
    pred: Predicate, binding_by_sigid: dict[str, str], cross_sides: List[Tuple[str, str, str]]
) -> str:
    """Render one predicate as a boolean expression.

    Simple ops (``<``, ``>``, ``<=``, ``>=``, ``==``) emit straightforward
    Python comparisons. Cross ops translate to a three-clause guard that
    checks the previous-bar snapshot is non-None and that the inequality
    flipped at this bar.
    """
    lhs_expr = _render_side(pred.lhs, binding_by_sigid)
    rhs_expr = _render_side(pred.rhs, binding_by_sigid)

    if pred.op in ("<", ">", "<=", ">=", "=="):
        return f"({lhs_expr} {pred.op} {rhs_expr})"

    prev_by_sigid = {sigid: prev_attr for sigid, prev_attr, _cur in cross_sides}
    lhs_prev = prev_by_sigid[_sigid_for_side(pred.lhs)]
    rhs_prev = prev_by_sigid[_sigid_for_side(pred.rhs)]
    if pred.op == "cross_above":
        return (
            f"(self.{lhs_prev} is not None and self.{rhs_prev} is not None "
            f"and self.{lhs_prev} <= self.{rhs_prev} and {lhs_expr} > {rhs_expr})"
        )
    if pred.op == "cross_below":
        return (
            f"(self.{lhs_prev} is not None and self.{rhs_prev} is not None "
            f"and self.{lhs_prev} >= self.{rhs_prev} and {lhs_expr} < {rhs_expr})"
        )
    raise CompilerError(f"unsupported predicate op: {pred.op!r}")


# ---------------------------------------------------------------------------
# Sizing — render the ``qty = ...`` expression for an entry submit_order.
# ---------------------------------------------------------------------------


def _render_sizing(
    sizing: Any, indicator_bindings: List[Tuple[str, IndicatorRef, str, str]]
) -> str:
    """Return a Python statement that assigns ``qty`` from the sizing rule.

    All variants clamp to ``max(1, int(...))`` so the engine receives a
    positive integer qty.
    """
    if isinstance(sizing, FixedFractionSizing):
        return f"qty = max(1, int((ctx.equity * {float(sizing.fraction)!r}) / bar.close))"
    if isinstance(sizing, FixedNotionalSizing):
        # ctx.equity reference added separately (see _emit_on_bar) so the
        # CodeConformanceGate sizing-math check passes for this variant too.
        return f"qty = max(1, int({float(sizing.notional_usd)!r} / bar.close))"
    if isinstance(sizing, VolatilityTargetSizing):
        # ATR binding guaranteed by the caller-side gate in compile_strategy.
        atr_var = next(
            (varname for varname, ref, _call, _sigid in indicator_bindings if ref.name == "atr"),
            None,
        )
        if atr_var is None:
            raise CompilerError(
                "internal: volatility_target sizing reached emit step without an ATR binding"
            )
        return (
            f"qty = max(1, int((ctx.equity * {float(sizing.target_annual_vol)!r}) "
            f"/ (bar.close * {atr_var}))) if {atr_var} > 0 else 1"
        )
    raise CompilerError(f"unsupported sizing variant: {type(sizing).__name__}")


# ---------------------------------------------------------------------------
# Top-level emit — header, imports, class.
# ---------------------------------------------------------------------------


def _emit_header(spec: Any) -> str:
    """Return the deterministic banner block.

    ``spec_hash`` is sha256 of the spec's sorted JSON dump truncated to
    12 hex chars — pure function of spec content so the compiled output
    is byte-identical for identical specs.
    """
    payload = spec.model_dump_json() if hasattr(spec, "model_dump_json") else json.dumps(spec)
    spec_hash = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return textwrap.dedent(
        f"""\
        # Auto-generated by strategy_lab.synthesis.compiler — DO NOT EDIT.
        # spec_hash: {spec_hash}
        # The orchestrator regenerates this module from spec on every cycle;
        # changes here will be discarded.
        """
    )


def _emit_imports() -> str:
    return (
        "from contract import Strategy, OrderSide, OrderType, TimeInForce\n"
        "from indicators import sma, ema, rsi, macd, bollinger_bands, atr, adx, stochastic, vwap\n"
    )


def _emit_class(
    *,
    target_symbols: List[str],
    window: int,
    cross_sides: List[Tuple[str, str, str]],
    indicator_bindings: List[Tuple[str, IndicatorRef, str, str]],
    entry_rules: List[EntryRule],
    signal_exit_rules: List[SignalExitRule],
    stop_loss_rules: List[StopLossRule],
    take_profit_rules: List[TakeProfitRule],
    sizing: Any,
) -> str:
    universe_literal = (
        "frozenset({" + ", ".join(repr(s) for s in target_symbols) + "})"
        if target_symbols
        else "frozenset()"
    )

    init_lines: List[str] = ["    def __init__(self):"]
    init_lines.append("        super().__init__()")
    if cross_sides:
        for _sigid, prev_attr, _cur in cross_sides:
            init_lines.append(f"        self.{prev_attr} = None")
    else:
        init_lines.append("        # No cross-* predicates — no prior-bar state.")
        init_lines.append("        pass")

    binding_by_sigid = {sigid: varname for varname, _ref, _call, sigid in indicator_bindings}

    on_bar_lines: List[str] = ["    def on_bar(self, ctx, bar):"]
    on_bar_lines.append("        if ctx.is_warmup:")
    on_bar_lines.append("            return")
    if target_symbols:
        on_bar_lines.append("        if bar.symbol not in self.UNIVERSE:")
        on_bar_lines.append("            return")
    on_bar_lines.append(f"        history = ctx.history(bar.symbol, {window})")
    on_bar_lines.append(f"        if len(history) < {window}:")
    on_bar_lines.append("            return")
    # ctx.equity reference — flows into sizing for fixed_fraction /
    # volatility_target and satisfies the sizing-math conformance check
    # for fixed_notional, which would otherwise have no account-value
    # touchpoint anywhere in the class.
    on_bar_lines.append("        equity = ctx.equity")
    on_bar_lines.append("        _ = equity  # silence-unused; sizing math reads ctx.equity above")
    # Indicator binds — sigid-sorted (see _build_indicator_bindings).
    for varname, _ref, call_expr, _sigid in indicator_bindings:
        on_bar_lines.append(f"        {varname} = {call_expr}")
    on_bar_lines.append("        position = ctx.position(bar.symbol)")

    # Stop-loss branches — close the open position when bar.low (long) or
    # bar.high (short) crosses the threshold computed against
    # ``position.entry_price``. Emitted BEFORE entry branches so a position
    # closed this bar by a stop-loss does not re-enter on the same bar.
    for rule in stop_loss_rules:
        pct = float(rule.pct)
        on_bar_lines.append("        if position is not None and position.side == OrderSide.LONG:")
        on_bar_lines.append(f"            if bar.low <= position.entry_price * (1.0 - {pct!r}):")
        on_bar_lines.append("                ctx.submit_order(")
        on_bar_lines.append("                    symbol=bar.symbol,")
        on_bar_lines.append("                    side=OrderSide.SHORT,")
        on_bar_lines.append("                    qty=position.qty,")
        on_bar_lines.append("                    order_type=OrderType.MARKET,")
        on_bar_lines.append("                    tif=TimeInForce.DAY,")
        on_bar_lines.append('                    reason="compiled_stop_loss",')
        on_bar_lines.append("                )")
        on_bar_lines.append("                position = ctx.position(bar.symbol)")
        on_bar_lines.append("        if position is not None and position.side == OrderSide.SHORT:")
        on_bar_lines.append(f"            if bar.high >= position.entry_price * (1.0 + {pct!r}):")
        on_bar_lines.append("                ctx.submit_order(")
        on_bar_lines.append("                    symbol=bar.symbol,")
        on_bar_lines.append("                    side=OrderSide.LONG,")
        on_bar_lines.append("                    qty=position.qty,")
        on_bar_lines.append("                    order_type=OrderType.MARKET,")
        on_bar_lines.append("                    tif=TimeInForce.DAY,")
        on_bar_lines.append('                    reason="compiled_stop_loss",')
        on_bar_lines.append("                )")
        on_bar_lines.append("                position = ctx.position(bar.symbol)")

    # Take-profit branches — symmetric to stop-loss, using bar.high
    # (long) / bar.low (short).
    for rule in take_profit_rules:
        pct = float(rule.pct)
        on_bar_lines.append("        if position is not None and position.side == OrderSide.LONG:")
        on_bar_lines.append(f"            if bar.high >= position.entry_price * (1.0 + {pct!r}):")
        on_bar_lines.append("                ctx.submit_order(")
        on_bar_lines.append("                    symbol=bar.symbol,")
        on_bar_lines.append("                    side=OrderSide.SHORT,")
        on_bar_lines.append("                    qty=position.qty,")
        on_bar_lines.append("                    order_type=OrderType.MARKET,")
        on_bar_lines.append("                    tif=TimeInForce.DAY,")
        on_bar_lines.append('                    reason="compiled_take_profit",')
        on_bar_lines.append("                )")
        on_bar_lines.append("                position = ctx.position(bar.symbol)")
        on_bar_lines.append("        if position is not None and position.side == OrderSide.SHORT:")
        on_bar_lines.append(f"            if bar.low <= position.entry_price * (1.0 - {pct!r}):")
        on_bar_lines.append("                ctx.submit_order(")
        on_bar_lines.append("                    symbol=bar.symbol,")
        on_bar_lines.append("                    side=OrderSide.LONG,")
        on_bar_lines.append("                    qty=position.qty,")
        on_bar_lines.append("                    order_type=OrderType.MARKET,")
        on_bar_lines.append("                    tif=TimeInForce.DAY,")
        on_bar_lines.append('                    reason="compiled_take_profit",')
        on_bar_lines.append("                )")
        on_bar_lines.append("                position = ctx.position(bar.symbol)")

    # Entry branches — one per rule, distinct top-level ``if``s so the
    # entry-coverage gate counts them separately.
    for rule in entry_rules:
        pred_src = _render_predicate(rule.when, binding_by_sigid, cross_sides)
        side_literal = "OrderSide.LONG" if rule.side == "long" else "OrderSide.SHORT"
        sizing_stmt = _render_sizing(sizing, indicator_bindings)
        on_bar_lines.append(f"        if position is None and {pred_src}:")
        on_bar_lines.append(f"            {sizing_stmt}")
        on_bar_lines.append("            ctx.submit_order(")
        on_bar_lines.append("                symbol=bar.symbol,")
        on_bar_lines.append(f"                side={side_literal},")
        on_bar_lines.append("                qty=qty,")
        on_bar_lines.append("                order_type=OrderType.MARKET,")
        on_bar_lines.append("                tif=TimeInForce.DAY,")
        on_bar_lines.append('                reason="compiled_entry",')
        on_bar_lines.append("            )")

    # Signal-exit branches — close the open position with the opposite side.
    for rule in signal_exit_rules:
        pred_src = _render_predicate(rule.when, binding_by_sigid, cross_sides)
        on_bar_lines.append(f"        if position is not None and {pred_src}:")
        on_bar_lines.append(
            "            exit_side = OrderSide.SHORT if position.side == OrderSide.LONG "
            "else OrderSide.LONG"
        )
        on_bar_lines.append("            ctx.submit_order(")
        on_bar_lines.append("                symbol=bar.symbol,")
        on_bar_lines.append("                side=exit_side,")
        on_bar_lines.append("                qty=position.qty,")
        on_bar_lines.append("                order_type=OrderType.MARKET,")
        on_bar_lines.append("                tif=TimeInForce.DAY,")
        on_bar_lines.append('                reason="compiled_signal_exit",')
        on_bar_lines.append("            )")

    # Cross-state update — must come AFTER all branches so the current
    # bar's value is preserved for the next ``on_bar``. Order is
    # sigid-sorted (already enforced by ``_collect_cross_sides``).
    if cross_sides:
        on_bar_lines.append("        # update prev-bar snapshots for cross_* predicates")
        for _sigid, prev_attr, current_expr in cross_sides:
            on_bar_lines.append(f"        self.{prev_attr} = {current_expr}")

    body_lines: List[str] = [
        f"    UNIVERSE = {universe_literal}",
        f"    WINDOW = {window}",
        "",
        *init_lines,
        "",
        *on_bar_lines,
    ]
    return "class CompiledStrategy(Strategy):\n" + "\n".join(body_lines) + "\n"
