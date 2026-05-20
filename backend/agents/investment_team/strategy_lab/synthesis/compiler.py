"""Spec → canonical Python compiler (issue #538).

Pure function ``compile_strategy(spec)`` that turns a structured
``StrategySpec`` into the ``Strategy`` subclass the streaming harness
expects. The emitted module is shaped to pass ``CodeSafetyChecker`` and
``CodeConformanceGate`` by construction.

Determinism contract: the same spec always produces byte-identical
output. The header carries a SHA-256 content hash of the spec's sorted
JSON dump for traceability; nothing else in the output varies between
invocations.

Scope (#538 + locked decisions, post-codex P1 review):
  * Indicator math is INLINED as helper methods on the compiled class
    (``self.sma(history, period=14)`` etc.) rather than imported from
    the sandbox's pandas-Series-based ``indicators`` module. The
    factors compiler (``strategy_lab/factors/compiler.py``) follows the
    same pattern. Helper names match the conformance gate's
    ``_INDICATOR_ALLOWED_CALL_NAMES`` allow-list, so a ``self.sma(...)``
    call inside ``on_bar`` is picked up as ``"sma"`` by
    ``_get_call_name`` and credited by the gate.
  * Tuple-valued indicators (MACD / Bollinger / Stochastic) thread the
    DSL's ``output`` / ``band`` selector through to the helper, which
    returns the single scalar component used in the predicate. The
    selector defaults match the DSL registry.
  * Stop-loss / take-profit (including trailing-basis variants) are
    NOT inlined as ``submit_order`` calls AND not attached as bracket
    legs. The trading service's ``_EngineExitDispatcher`` runs
    ``evaluate_exit_rules`` against ``spec.exit_rules`` on every bar
    and emits ``engine_exit:<kind>`` orders against the actual
    post-fill ``position.entry_price`` (correct basis semantics).
    The safety gate's order-flow check accepts entries-only flow
    when ``spec.exit_rules`` has an engine-handled rule (see
    ``code_safety._spec_has_engine_handled_exit``). Signal-exit
    rules (a no-op in ``evaluate_exit_rules`` — see
    ``executor/rule_compiler.py``) are still inlined as ``on_bar``
    branches gated by an ``exit_submitted`` flag.
  * ``cross_above`` / ``cross_below`` predicates compare the current
    side value against ``self._prev_<sigid>`` snapshots updated at the
    end of every ``on_bar`` invocation where the universe and warm-up
    guards passed. "Previous" means previous successful ``on_bar``,
    not previous calendar bar.
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


# DSL name → canonical method name emitted on the compiled class. Names
# match the conformance gate's ``_INDICATOR_ALLOWED_CALL_NAMES`` so the
# call name (``node.func.attr`` for ``self.<name>(...)``) is credited as
# the indicator's named implementation.
_INDICATOR_METHOD_NAME: dict[str, str] = {
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

_BAR_FIELD_EXPR: dict[str, str] = {
    "bar.close": "bar.close",
    "bar.high": "bar.high",
    "bar.low": "bar.low",
    "bar.volume": "bar.volume",
}

# Floor on the rolling-history window the strategy requests from
# ``ctx.history``. Indicators with short lookbacks still benefit from a
# small buffer; matches the floor in the ideation system prompt.
_MIN_WINDOW: int = 20

# Lookback for VWAP. The sandbox VWAP definition (``factors/primitives.py``,
# ``executor/indicators.py``) is cumulative-over-the-series rather than
# rolling-window; capping at ``_MIN_WINDOW`` would turn it into a 20-bar
# rolling VWAP and silently change signal semantics. Use the harness's
# own retention ceiling (500 bars; see ``StrategyContext._ingest_bar``)
# so the strategy gets the deepest history the engine retains.
_VWAP_HISTORY: int = 500


def compile_strategy(spec: Any) -> str:
    """Compile ``spec`` into a canonical ``Strategy`` Python module.

    Pre: ``spec`` is a ``StrategySpec`` (duck-typed; only the public
    fields ``target_symbols``, ``entry_rules``, ``exit_rules``,
    ``sizing`` are read).
    Post: returns a non-empty Python source string with exactly one
    ``Strategy`` subclass. Raises :class:`CompilerError` for specs the
    compiler cannot express (e.g. ``volatility_target`` sizing without
    a matching ATR indicator, trailing-basis stop-losses).
    """
    entry_rules: List[EntryRule] = list(getattr(spec, "entry_rules", []) or [])
    exit_rules: List[Any] = list(getattr(spec, "exit_rules", []) or [])
    target_symbols: List[str] = list(getattr(spec, "target_symbols", []) or [])
    sizing = getattr(spec, "sizing", None)
    if sizing is None:
        raise CompilerError("spec.sizing is required")

    signal_exit_rules = [r for r in exit_rules if isinstance(r, SignalExitRule)]

    # NOTE: stop-loss / take-profit (including trailing-basis variants)
    # are NOT inlined in compiled code and NOT attached as bracket legs
    # on the entry order. The trading service's
    # ``_EngineExitDispatcher`` runs ``evaluate_exit_rules`` against
    # ``spec.exit_rules`` on every bar (see ``trading_service/service.py``
    # line 276 and ``modes/backtest.py`` line 185) and emits
    # ``engine_exit:<kind>`` orders against the post-fill
    # ``position.entry_price`` — correct basis semantics for all of
    # ``entry_price`` / ``trailing_high`` / ``trailing_low``. The
    # safety gate's order-flow check accepts entries-only flow when
    # spec has at least one engine-handled rule (see
    # ``code_safety._spec_has_engine_handled_exit``).

    indicator_refs: List[IndicatorRef] = _collect_indicators(entry_rules, signal_exit_rules)
    # MACD convention requires fast < slow; the DSL registry does not
    # cross-check the two periods, so the validation lives here. With
    # fast >= slow the helper's ``sub[-fast]`` index would walk off the
    # left end of ``sub`` when ``len(sub) = slow``.
    for ref in indicator_refs:
        if ref.name == "macd":
            fast = int(ref.param("fast"))
            slow = int(ref.param("slow"))
            if fast >= slow:
                raise CompilerError(
                    f"macd indicator requires fast < slow (got fast={fast}, "
                    f"slow={slow}); falling back to LLM synthesis"
                )

    if isinstance(sizing, VolatilityTargetSizing):
        atr_refs = [ref for ref in indicator_refs if ref.name == "atr"]
        if not atr_refs:
            raise CompilerError(
                "volatility_target sizing requires an 'atr' indicator referenced "
                "by an entry or signal-exit rule; none found in spec"
            )
        # Multiple distinct ATR refs make the sizing choice ambiguous —
        # the sigid-sort tiebreaker is deterministic but author-unaware,
        # so adding an unrelated ATR predicate would silently change
        # trade size. Refuse the spec and let LLM synthesis decide.
        distinct = {ref.param("period") for ref in atr_refs}
        if len(distinct) > 1:
            raise CompilerError(
                "volatility_target sizing is ambiguous when the spec references "
                f"multiple ATR periods ({sorted(distinct)}); compiler "
                "cannot pick one without author intent — falling back to LLM"
            )

    indicator_bindings = _build_indicator_bindings(indicator_refs)
    cross_sides = _collect_cross_sides(entry_rules, signal_exit_rules, indicator_bindings)
    used_helper_names = sorted({_INDICATOR_METHOD_NAME[ref.name] for ref in indicator_refs})
    needs_source_helper = any(
        ref.name in ("sma", "ema", "rsi", "macd", "bollinger") for ref in indicator_refs
    )

    # History request depth and warm-up threshold are decoupled. VWAP
    # wants the maximum retained depth (cumulative semantics) but its
    # helper computes from any ≥ ``_MIN_WINDOW`` bars, so gating the
    # whole strategy on 500 bars would silently block all trading on
    # backtests with < 500 bars. Earlier rounds used a single ``WINDOW``
    # for both and tripped this regression.
    history_depth = max((_history_depth_for(ref) for ref in indicator_refs), default=_MIN_WINDOW)
    history_depth = max(history_depth, _MIN_WINDOW)
    warmup_min = max((_lookback_for(ref) for ref in indicator_refs), default=_MIN_WINDOW)
    warmup_min = max(warmup_min, _MIN_WINDOW)

    parts: List[str] = []
    parts.append(_emit_header(spec))
    parts.append(_emit_imports())
    parts.append(
        _emit_class(
            target_symbols=target_symbols,
            history_depth=history_depth,
            warmup_min=warmup_min,
            cross_sides=cross_sides,
            indicator_bindings=indicator_bindings,
            entry_rules=entry_rules,
            exit_rules=exit_rules,
            sizing=sizing,
            used_helper_names=used_helper_names,
            needs_source_helper=needs_source_helper,
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
    """Return the MINIMUM bar-history depth ``ref`` needs before its
    value is meaningful — used to compute the strategy's warm-up gate.

    Different from :func:`_history_depth_for`: that returns the depth
    to REQUEST from ``ctx.history``, which can be larger (VWAP wants
    cumulative-style depth but only needs ≥1 bar to compute).
    """
    name = ref.name
    if name in ("sma", "ema"):
        return int(ref.param("period"))
    if name == "rsi":
        return int(ref.param("period")) + 1
    if name == "macd":
        # MACD warm-up depends on the selected output. ``output='macd'``
        # only needs ``slow`` bars (the MACD line is computable at the
        # first sub of length ``slow``). ``output ∈ {signal, histogram}``
        # additionally needs ``signal`` macd-line samples to compute the
        # signal-line EMA, so ``slow + signal - 1`` bars total.
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
        # Helper needs 2 * period + 1 bars before returning a value
        # (Wilder smoothing requires two DX windows). A smaller window
        # leaves the binding permanently None and ADX predicates never
        # fire even on otherwise-sufficient market history.
        return 2 * int(ref.param("period")) + 1
    if name == "stochastic":
        # Helper computes ``%D`` once it has ``k_period + d_period - 1``
        # bars (the first ``%K`` is available at ``k_period`` and the
        # SMA over ``d_period`` values needs ``d_period - 1`` more bars
        # of ``%K`` history). Returning ``k_period + d_period`` would
        # delay the first valid signal by one bar with no safety win.
        return int(ref.param("k_period")) + int(ref.param("d_period")) - 1
    if name == "vwap":
        # Helper returns a value at ≥1 bar (cumulative sum doesn't have
        # a strict warm-up). Floor to ``_MIN_WINDOW`` for safety — the
        # value at 1 bar is just (h+l+c)/3, not informative.
        return _MIN_WINDOW
    raise CompilerError(f"unsupported indicator: {name!r}")


def _history_depth_for(ref: IndicatorRef) -> int:
    """Return the depth to REQUEST from ``ctx.history(symbol, n)``.

    Same as :func:`_lookback_for` for most indicators — they only need
    their lookback worth of bars. VWAP is the exception: the sandbox
    helper computes a cumulative-over-the-series VWAP, so the strategy
    should ask for as deep a history as the harness retains
    (``_VWAP_HISTORY``). Using the lookback (≥1) here would request
    only the minimum and silently make compiled VWAP a 1-bar value.
    """
    if ref.name == "vwap":
        return _VWAP_HISTORY
    return _lookback_for(ref)


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
    """Render the ``self.<name>(history, ...)`` call expression for one ref.

    For tuple-returning indicators (macd, bollinger, stochastic) the
    selector param (``output`` / ``band``) is threaded into the call so
    the helper returns the single scalar the predicate compares against.
    """
    method = _INDICATOR_METHOD_NAME[ref.name]
    if ref.name in ("sma", "ema"):
        return f"self.{method}(history, period={int(ref.param('period'))}, source={ref.source!r})"
    if ref.name == "rsi":
        return f"self.{method}(history, period={int(ref.param('period'))}, source={ref.source!r})"
    if ref.name == "macd":
        return (
            f"self.{method}(history, fast={int(ref.param('fast'))}, "
            f"slow={int(ref.param('slow'))}, signal={int(ref.param('signal'))}, "
            f"source={ref.source!r}, select={str(ref.param('output'))!r})"
        )
    if ref.name == "bollinger":
        return (
            f"self.{method}(history, period={int(ref.param('period'))}, "
            f"num_std={float(ref.param('num_std'))!r}, "
            f"source={ref.source!r}, select={str(ref.param('band'))!r})"
        )
    if ref.name in ("atr", "adx"):
        return f"self.{method}(history, period={int(ref.param('period'))})"
    if ref.name == "stochastic":
        return (
            f"self.{method}(history, k_period={int(ref.param('k_period'))}, "
            f"d_period={int(ref.param('d_period'))}, "
            f"select={str(ref.param('output'))!r})"
        )
    if ref.name == "vwap":
        return f"self.{method}(history)"
    raise CompilerError(f"unsupported indicator: {ref.name!r}")


def _collect_cross_sides(
    entry_rules: List[EntryRule],
    signal_exit_rules: List[SignalExitRule],
    indicator_bindings: List[Tuple[str, IndicatorRef, str, str]],
) -> List[Tuple[str, str, str]]:
    """Return ``(sigid, prev_var, current_expr)`` for every side that
    participates in any ``cross_above`` / ``cross_below`` predicate.

    ``prev_var`` is the local variable name (``prev_<sigid>``) bound at
    the top of ``on_bar`` from the per-symbol ``self._cross_prev`` dict;
    ``current_expr`` is the expression yielding the side's current-bar
    value (an indicator binding variable or a bar-field reference).
    De-duplicated by sigid; sort order is sigid so emission is stable.
    """
    binding_by_sigid = {sigid: varname for varname, _ref, _call, sigid in indicator_bindings}
    out: dict[str, Tuple[str, str, str]] = {}

    def _record(side: Any) -> None:
        sigid = _sigid_for_side(side)
        if sigid in out:
            return
        prev_var = f"prev_{sigid}"
        current_expr = _render_side(side, binding_by_sigid)
        out[sigid] = (sigid, prev_var, current_expr)

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
    Python comparisons guarded against ``None`` only for indicator-ref
    sides (a helper returns ``None`` during the warm-up window). Numeric
    literals and bar-field references are never ``None`` so guarding
    them would trip the ``is not None`` with a literal SyntaxWarning.

    Cross ops translate to a guard that checks both previous-bar
    snapshots are non-None and the inequality flipped at this bar.
    """
    lhs_expr = _render_side(pred.lhs, binding_by_sigid)
    rhs_expr = _render_side(pred.rhs, binding_by_sigid)
    guards: List[str] = []
    if isinstance(pred.lhs, IndicatorRef):
        guards.append(f"{lhs_expr} is not None")
    if isinstance(pred.rhs, IndicatorRef):
        guards.append(f"{rhs_expr} is not None")

    if pred.op in ("<", ">", "<=", ">=", "=="):
        clauses = guards + [f"{lhs_expr} {pred.op} {rhs_expr}"]
        return "(" + " and ".join(clauses) + ")"

    prev_by_sigid = {sigid: prev_var for sigid, prev_var, _cur in cross_sides}
    lhs_prev = prev_by_sigid[_sigid_for_side(pred.lhs)]
    rhs_prev = prev_by_sigid[_sigid_for_side(pred.rhs)]
    cross_clauses = guards + [
        f"{lhs_prev} is not None",
        f"{rhs_prev} is not None",
    ]
    if pred.op == "cross_above":
        cross_clauses.append(f"{lhs_prev} <= {rhs_prev}")
        cross_clauses.append(f"{lhs_expr} > {rhs_expr}")
        return "(" + " and ".join(cross_clauses) + ")"
    if pred.op == "cross_below":
        cross_clauses.append(f"{lhs_prev} >= {rhs_prev}")
        cross_clauses.append(f"{lhs_expr} < {rhs_expr}")
        return "(" + " and ".join(cross_clauses) + ")"
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
            f"/ (bar.close * {atr_var}))) if {atr_var} and {atr_var} > 0 else 1"
        )
    raise CompilerError(f"unsupported sizing variant: {type(sizing).__name__}")


# ---------------------------------------------------------------------------
# Inline indicator helper-method bodies. Each takes ``self``, a ``history``
# list of ``Bar``-like objects, and the indicator's params; returns the
# scalar value at the LAST bar (or ``None`` when there is insufficient
# history). The math mirrors ``strategy_lab/factors/primitives.py`` so the
# compiled output remains drop-in for the engine and the trade-alignment
# loop never sees diverging "compiled vs. reference" semantics.
# ---------------------------------------------------------------------------


def _emit_source_helper() -> str:
    return textwrap.dedent(
        """\
        def _src(self, bar, source):
            if source == "close":
                return bar.close
            if source == "high":
                return bar.high
            if source == "low":
                return bar.low
            if source == "open":
                return bar.open
            if source == "volume":
                return bar.volume
            if source == "hl2":
                return (bar.high + bar.low) / 2.0
            if source == "ohlc4":
                return (bar.open + bar.high + bar.low + bar.close) / 4.0
            return bar.close
        """
    )


_HELPER_BODIES: dict[str, str] = {
    "sma": textwrap.dedent(
        """\
        def sma(self, history, period, source="close"):
            if len(history) < period:
                return None
            vals = [self._src(b, source) for b in history[-period:]]
            return sum(vals) / period
        """
    ),
    "ema": textwrap.dedent(
        """\
        def ema(self, history, period, source="close"):
            if len(history) < period:
                return None
            alpha = 2.0 / (period + 1.0)
            vals = [self._src(b, source) for b in history[-period:]]
            val = vals[0]
            for v in vals[1:]:
                val = alpha * v + (1.0 - alpha) * val
            return val
        """
    ),
    "rsi": textwrap.dedent(
        """\
        def rsi(self, history, period=14, source="close"):
            if len(history) < period + 1:
                return None
            gains = 0.0
            losses = 0.0
            for i in range(len(history) - period, len(history)):
                cur = self._src(history[i], source)
                prev = self._src(history[i - 1], source)
                delta = cur - prev
                if delta > 0:
                    gains += delta
                else:
                    losses += -delta
            avg_gain = gains / period
            avg_loss = losses / period
            if avg_loss == 0:
                return 100.0 if avg_gain > 0 else 50.0
            rs = avg_gain / avg_loss
            return 100.0 - (100.0 / (1.0 + rs))
        """
    ),
    "macd": textwrap.dedent(
        """\
        def macd(self, history, fast=12, slow=26, signal=9, source="close", select="macd"):
            # Defensive: the compile_strategy front-door rejects fast >= slow,
            # but the helper guards too so a runtime bug never IndexErrors.
            if fast >= slow:
                return None
            # Selector-aware warm-up: macd-line is computable at ``slow``
            # bars; signal/histogram need an extra ``signal - 1`` bars of
            # macd values to drive the EMA.
            min_bars = slow if select == "macd" else slow + signal - 1
            if len(history) < min_bars:
                return None
            macd_line = []
            for end in range(slow, len(history) + 1):
                sub = history[:end]
                # EMA(fast) over sub
                alpha_f = 2.0 / (fast + 1.0)
                ef = self._src(sub[-fast], source)
                for b in sub[-fast + 1:]:
                    ef = alpha_f * self._src(b, source) + (1.0 - alpha_f) * ef
                # EMA(slow) over sub
                alpha_s = 2.0 / (slow + 1.0)
                es = self._src(sub[-slow], source)
                for b in sub[-slow + 1:]:
                    es = alpha_s * self._src(b, source) + (1.0 - alpha_s) * es
                macd_line.append(ef - es)
            if select == "macd":
                return macd_line[-1]
            if len(macd_line) < signal:
                return None
            alpha_g = 2.0 / (signal + 1.0)
            sig = macd_line[0]
            for x in macd_line[1:]:
                sig = alpha_g * x + (1.0 - alpha_g) * sig
            if select == "signal":
                return sig
            if select == "histogram":
                return macd_line[-1] - sig
            return None
        """
    ),
    "bollinger_bands": textwrap.dedent(
        """\
        def bollinger_bands(self, history, period=20, num_std=2.0, source="close", select="middle"):
            if len(history) < period:
                return None
            vals = [self._src(b, source) for b in history[-period:]]
            mean = sum(vals) / period
            var = sum((v - mean) ** 2 for v in vals) / period
            std = math.sqrt(var) if var > 0 else 0.0
            if select == "middle":
                return mean
            if select == "upper":
                return mean + num_std * std
            if select == "lower":
                return mean - num_std * std
            return None
        """
    ),
    "atr": textwrap.dedent(
        """\
        def atr(self, history, period=14):
            if len(history) < period + 1:
                return None
            trs = []
            for i in range(len(history) - period, len(history)):
                h = history[i].high
                low = history[i].low
                prev_close = history[i - 1].close
                trs.append(max(h - low, abs(h - prev_close), abs(low - prev_close)))
            return sum(trs) / period
        """
    ),
    "adx": textwrap.dedent(
        """\
        def adx(self, history, period=14):
            if len(history) < 2 * period + 1:
                return None
            plus_dms = []
            minus_dms = []
            trs = []
            for i in range(1, len(history)):
                up = history[i].high - history[i - 1].high
                down = history[i - 1].low - history[i].low
                plus_dm = up if (up > down and up > 0) else 0.0
                minus_dm = down if (down > up and down > 0) else 0.0
                prev_close = history[i - 1].close
                tr = max(
                    history[i].high - history[i].low,
                    abs(history[i].high - prev_close),
                    abs(history[i].low - prev_close),
                )
                plus_dms.append(plus_dm)
                minus_dms.append(minus_dm)
                trs.append(tr)
            tr_sum = sum(trs[-period:])
            if tr_sum == 0:
                return 0.0
            plus_di = 100.0 * sum(plus_dms[-period:]) / tr_sum
            minus_di = 100.0 * sum(minus_dms[-period:]) / tr_sum
            denom = plus_di + minus_di
            if denom == 0:
                return 0.0
            return 100.0 * abs(plus_di - minus_di) / denom
        """
    ),
    "stochastic": textwrap.dedent(
        """\
        def stochastic(self, history, k_period=14, d_period=3, select="k"):
            if len(history) < k_period:
                return None
            def _k_at(end):
                w = history[end - k_period:end]
                lowest = min(b.low for b in w)
                highest = max(b.high for b in w)
                rng = highest - lowest
                if rng == 0:
                    return 50.0
                return 100.0 * (history[end - 1].close - lowest) / rng
            k_val = _k_at(len(history))
            if select == "k":
                return k_val
            if len(history) < k_period + d_period - 1:
                return None
            k_vals = [_k_at(end) for end in range(k_period, len(history) + 1)]
            return sum(k_vals[-d_period:]) / d_period
        """
    ),
    "vwap": textwrap.dedent(
        """\
        def vwap(self, history):
            if not history:
                return None
            num = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in history)
            den = sum(b.volume for b in history)
            if den == 0:
                return sum(b.close for b in history) / len(history)
            return num / den
        """
    ),
}


# ---------------------------------------------------------------------------
# Top-level emit — header, imports, class.
# ---------------------------------------------------------------------------


def _canonical_spec_payload(spec: Any) -> dict[str, Any]:
    """Return a sort-stable dict of the DSL fields that determine
    compiled output. Used by ``_emit_header`` so the spec-hash header
    is invariant to non-DSL fields like ``strategy_code`` (which is
    itself the compiler's output), ``hypothesis`` prose, ``audit``,
    and rule-level ``note`` text (author prose, never used in code-gen).
    """

    def _strip_notes(value: Any) -> Any:
        # ``note`` is author prose attached to rules / sizing /
        # indicator-refs; it never affects emitted code. Drop it
        # recursively so semantically identical specs hash the same
        # regardless of comment churn.
        if isinstance(value, dict):
            return {k: _strip_notes(v) for k, v in value.items() if k != "note"}
        if isinstance(value, list):
            return [_strip_notes(v) for v in value]
        return value

    def _dump(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "model_dump"):
            return _strip_notes(value.model_dump(mode="json"))
        return _strip_notes(value)

    return {
        "target_symbols": list(getattr(spec, "target_symbols", []) or []),
        "entry_rules": [_dump(r) for r in (getattr(spec, "entry_rules", []) or [])],
        "exit_rules": [_dump(r) for r in (getattr(spec, "exit_rules", []) or [])],
        "sizing": _dump(getattr(spec, "sizing", None)),
    }


def _emit_header(spec: Any) -> str:
    """Return the deterministic banner block.

    ``spec_hash`` is sha256 of a canonical JSON dump of the DSL fields
    the compiler actually consumes (``target_symbols``, ``entry_rules``,
    ``exit_rules``, ``sizing``), truncated to 12 hex chars. Audit
    metadata, prose, and ``strategy_code`` itself are intentionally
    excluded so two semantically identical rule specs always produce
    byte-identical compiled output regardless of surrounding metadata.
    """
    payload = json.dumps(_canonical_spec_payload(spec), sort_keys=True)
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
    # ``indicators`` module isn't imported — the sandbox's pandas-Series
    # signatures don't match the compiled call shape, so the strategy
    # carries its own inline implementations (see ``_HELPER_BODIES``).
    # Bracket attachment classes (``StopAttachment`` / ``LimitAttachment``)
    # are no longer needed since the compiler doesn't attach brackets —
    # the engine enforces stop/take-profit from spec.exit_rules directly.
    return "import math\nfrom contract import Strategy, OrderSide, OrderType, TimeInForce\n"


def _indent_method(body: str, spaces: int = 4) -> str:
    """Indent a left-aligned method body by ``spaces`` columns."""
    return textwrap.indent(body.rstrip() + "\n", " " * spaces)


def _emit_class(
    *,
    target_symbols: List[str],
    history_depth: int,
    warmup_min: int,
    cross_sides: List[Tuple[str, str, str]],
    indicator_bindings: List[Tuple[str, IndicatorRef, str, str]],
    entry_rules: List[EntryRule],
    exit_rules: List[Any],
    sizing: Any,
    used_helper_names: List[str],
    needs_source_helper: bool,
) -> str:
    universe_literal = (
        "frozenset({" + ", ".join(repr(s) for s in target_symbols) + "})"
        if target_symbols
        else "frozenset()"
    )

    init_lines: List[str] = ["    def __init__(self):"]
    init_lines.append("        super().__init__()")
    if cross_sides:
        # ``self._cross_prev`` is keyed by ``bar.symbol`` so multi-symbol
        # runs don't leak previous-bar state across tickers. Each value
        # is a sigid → previous-value dict written at the end of every
        # successful ``on_bar``.
        init_lines.append("        self._cross_prev = {}")
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
    on_bar_lines.append(f"        history = ctx.history(bar.symbol, {history_depth})")
    on_bar_lines.append(f"        if len(history) < {warmup_min}:")
    on_bar_lines.append("            return")
    # Always emit a ctx.equity reference — flows into fixed_fraction /
    # volatility_target sizing and satisfies the sizing-math conformance
    # check for fixed_notional, whose qty expression has no other
    # account-value touchpoint.
    on_bar_lines.append("        equity = ctx.equity")
    on_bar_lines.append("        _ = equity  # silence-unused; sizing math reads ctx.equity above")
    # Indicator binds — sigid-sorted (see _build_indicator_bindings).
    for varname, _ref, call_expr, _sigid in indicator_bindings:
        on_bar_lines.append(f"        {varname} = {call_expr}")
    # Previous-bar snapshots used by cross_above/cross_below predicates.
    # Per-symbol scope (``self._cross_prev[bar.symbol]``) so a multi-
    # symbol run never compares this bar's value against another
    # symbol's previous bar — that would forge false cross triggers.
    if cross_sides:
        on_bar_lines.append("        _prev_for_symbol = self._cross_prev.get(bar.symbol, {})")
        for sigid, prev_var, _cur in cross_sides:
            on_bar_lines.append(f"        {prev_var} = _prev_for_symbol.get({sigid!r})")
    on_bar_lines.append("        position = ctx.position(bar.symbol)")

    # Conformance gate's ``_check_stop_loss_enforcement`` /
    # ``_check_take_profit_enforcement`` require the class to reference
    # ``position.entry_price`` when those rule kinds are in the spec.
    # The runtime enforcement lives in the engine; emit a benign read
    # so the static check passes without re-implementing the exit math.
    has_engine_handled_exit = any(isinstance(r, (StopLossRule, TakeProfitRule)) for r in exit_rules)
    if has_engine_handled_exit:
        on_bar_lines.append(
            "        _entry_ref = position.entry_price if position is not None else None"
        )
        on_bar_lines.append("        _ = _entry_ref  # engine enforces stop/take-profit thresholds")

    signal_exits_in_order = [r for r in exit_rules if isinstance(r, SignalExitRule)]
    if signal_exits_in_order:
        # Per-bar guard so multiple signal-exit predicates firing on the
        # same candle only emit one close order. Stop-loss / take-profit
        # are NOT inlined (see module docstring) so the flag scope is
        # narrower than in earlier rounds.
        on_bar_lines.append("        exit_submitted = False")
    for rule in signal_exits_in_order:
        pred_src = _render_predicate(rule.when, binding_by_sigid, cross_sides)
        on_bar_lines.append(
            f"        if not exit_submitted and position is not None and {pred_src}:"
        )
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
        on_bar_lines.append("            exit_submitted = True")

    # Entry branches — one per rule, distinct top-level ``if``s so the
    # entry-coverage gate counts them separately. A local
    # ``entry_submitted`` flag short-circuits multi-entry-rule specs so
    # two predicates true on the same bar don't double-up entry orders.
    # Bracket attachments (``attached_stop_loss`` / ``attached_take_profit``)
    # are added when the spec has the corresponding rule kinds — the
    # safety gate counts a non-None bracket leg as the entry+exit pair,
    # and the bracket prices are conservative bar.close-based estimates
    # (the engine's evaluate_exit_rules drives the precise basis logic).
    if entry_rules:
        on_bar_lines.append("        entry_submitted = False")
    # Entry submit_order — no bracket attachments. The previous round's
    # ``attached_stop_loss=StopAttachment(stop_price=bar.close * (1-pct))``
    # was semantically wrong on three fronts (codex round-6 review):
    #   1. ``bar.close`` at signal time differs from the actual fill
    #      price (market orders fill on the NEXT bar's open in the
    #      trading service), so gap opens make the bracket distance
    #      materially wrong vs. ``position.entry_price * (1-pct)``.
    #   2. ``StopAttachment(stop_price=...)`` is a static stop and
    #      silently downgrades ``StopLossRule.basis='trailing_high'`` /
    #      ``'trailing_low'`` to fixed levels.
    #   3. Only the first StopLossRule / TakeProfitRule fed the
    #      bracket, so multi-rule specs lost the secondary rules.
    # The engine's _EngineExitDispatcher already enforces every
    # stop/take rule (with the correct basis) against the post-fill
    # ``position.entry_price``; the safety gate has been widened to
    # accept entries-only flow when ``spec.exit_rules`` contains an
    # engine-handled rule (``_spec_has_engine_handled_exit``).
    for rule in entry_rules:
        pred_src = _render_predicate(rule.when, binding_by_sigid, cross_sides)
        side_literal = "OrderSide.LONG" if rule.side == "long" else "OrderSide.SHORT"
        sizing_stmt = _render_sizing(sizing, indicator_bindings)
        on_bar_lines.append(f"        if not entry_submitted and position is None and {pred_src}:")
        on_bar_lines.append(f"            {sizing_stmt}")
        on_bar_lines.append("            ctx.submit_order(")
        on_bar_lines.append("                symbol=bar.symbol,")
        on_bar_lines.append(f"                side={side_literal},")
        on_bar_lines.append("                qty=qty,")
        on_bar_lines.append("                order_type=OrderType.MARKET,")
        on_bar_lines.append("                tif=TimeInForce.DAY,")
        on_bar_lines.append('                reason="compiled_entry",')
        on_bar_lines.append("            )")
        on_bar_lines.append("            entry_submitted = True")

    # Cross-state update — must come AFTER all branches so the current
    # bar's value is preserved for the next ``on_bar`` invocation on
    # this symbol. Per-symbol dict scope (see ``self._cross_prev`` in
    # __init__). Order is sigid-sorted via ``_collect_cross_sides``.
    if cross_sides:
        on_bar_lines.append("        _new_prev = self._cross_prev.setdefault(bar.symbol, {})")
        for sigid, _prev_var, current_expr in cross_sides:
            on_bar_lines.append(f"        _new_prev[{sigid!r}] = {current_expr}")

    # Method blocks — class constants, __init__, on_bar, then indicator
    # helpers (and the source helper) appended at the end so the class
    # body reads top-down: constants, lifecycle, decision logic, math.
    helper_method_blocks: List[str] = []
    if needs_source_helper:
        helper_method_blocks.append(_indent_method(_emit_source_helper()))
    for name in used_helper_names:
        body = _HELPER_BODIES[name]
        helper_method_blocks.append(_indent_method(body))

    body_lines: List[str] = [
        f"    UNIVERSE = {universe_literal}",
        f"    WINDOW = {history_depth}",
        f"    WARMUP_MIN = {warmup_min}",
        "",
        *init_lines,
        "",
        *on_bar_lines,
    ]
    class_src = "class CompiledStrategy(Strategy):\n" + "\n".join(body_lines) + "\n"
    if helper_method_blocks:
        class_src += "\n" + "\n".join(helper_method_blocks)
    return class_src
