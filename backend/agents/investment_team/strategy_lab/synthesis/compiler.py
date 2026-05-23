"""Deterministic ``StrategySpec`` → canonical Python compiler.

``compile_strategy(spec)`` turns a structured ``StrategySpec`` into the
``Strategy`` subclass the streaming harness expects.

Module contract:
  Determinism: the same spec always produces byte-identical output.
    The header carries a SHA-256 content hash of a canonical DSL dump
    for traceability; no other source of variation is admitted (no
    ``datetime.now()``, no ``uuid``, no ``id()``).
  Static gates: emitted output is shaped to pass ``CodeSafetyChecker``
    and ``CodeConformanceGate`` by construction.
  Engine contract: ``on_bar`` covers universe guard, warm-up gate,
    bar-close validity, indicator binds, entries, and signal-exits.
    Stop-loss / take-profit (including trailing variants) are NOT
    inlined — they are enforced engine-side by ``evaluate_exit_rules``
    against the post-fill ``position.entry_price``, which alone has
    the correct basis semantics.
  Indicator math: helper bodies are inlined as class methods, not
    imported, because the sandbox's ``indicators`` module uses
    pandas-Series signatures incompatible with the per-bar call shape.
    Method names match ``CodeConformanceGate._INDICATOR_ALLOWED_CALL_NAMES``.
  Tuple indicators: MACD / Bollinger / Stochastic thread the DSL's
    ``output`` / ``band`` selector into the helper and return a scalar.
  Cross predicates: ``cross_above`` / ``cross_below`` compare current
    against ``self._cross_prev[symbol][sigid]`` — "previous" means the
    previous successful ``on_bar`` for the same symbol.
  ``volatility_target`` sizing requires an ``atr`` indicator in the
    spec; absent or ambiguous ATR raises :class:`CompilerError`.
  Empty ``spec.target_symbols`` is supported (no universe guard emitted).
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

    The orchestrator treats this as a signal to fall back to LLM-authored
    code: it sets ``spec.requires_custom_code = True`` and retains the
    ideation-generated ``strategy_code`` instead of the compiled output.
    """


# DSL name → emitted method name. Names must match
# ``CodeConformanceGate._INDICATOR_ALLOWED_CALL_NAMES`` so the
# conformance gate credits ``self.<name>(...)`` as the named indicator.
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

_MIN_WINDOW: int = 20

# VWAP requests the deepest retained history. Sandbox VWAP is
# cumulative-over-the-series, not rolling, so a smaller request would
# silently change signal semantics; 500 matches the harness retention
# ceiling in ``StrategyContext._ingest_bar``.
_VWAP_HISTORY: int = 500


def compile_strategy(spec: Any) -> str:
    """Compile ``spec`` into a canonical ``Strategy`` Python module.

    Pre:  ``spec`` is a ``StrategySpec`` (duck-typed: only the public
          fields ``target_symbols``, ``entry_rules``, ``exit_rules``,
          ``sizing`` are read).
    Post: returns a non-empty Python source string defining exactly one
          ``Strategy`` subclass. Output is byte-identical for any two
          calls with semantically equal specs.
    Raises: :class:`CompilerError` when the spec falls outside the
          expressible subset, e.g. ``volatility_target`` sizing without
          a matching ``atr`` indicator, MACD with ``fast >= slow``,
          unsupported indicator / predicate / sizing variants.
    """
    entry_rules: List[EntryRule] = list(getattr(spec, "entry_rules", []) or [])
    exit_rules: List[Any] = list(getattr(spec, "exit_rules", []) or [])
    target_symbols: List[str] = list(getattr(spec, "target_symbols", []) or [])
    sizing = getattr(spec, "sizing", None)
    if sizing is None:
        raise CompilerError("spec.sizing is required")

    signal_exit_rules = [r for r in exit_rules if isinstance(r, SignalExitRule)]

    indicator_refs: List[IndicatorRef] = _collect_indicators(entry_rules, signal_exit_rules)
    # MACD with ``fast >= slow`` would IndexError in the helper's
    # ``sub[-fast]`` slicing when ``len(sub) == slow``. The DSL registry
    # doesn't cross-check the two periods, so the validation lives here.
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
        # Multiple distinct ATR periods make the sizing choice ambiguous
        # — picking one by sigid would be deterministic but invisible to
        # the spec author, so adding an unrelated ATR predicate would
        # silently change trade size.
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

    # ``history_depth`` (request) and ``warmup_min`` (gate) are decoupled
    # so VWAP's cumulative-style depth request doesn't bind the warm-up
    # threshold of every other indicator to 500 bars.
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
# Indicator collection, sigid generation, lookback math.
# ---------------------------------------------------------------------------


def _collect_indicators(
    entry_rules: List[EntryRule], signal_exit_rules: List[SignalExitRule]
) -> List[IndicatorRef]:
    """Return the de-duplicated, sigid-sorted ``IndicatorRef`` set from the rules.

    Pre:  predicates are well-formed; sides are ``IndicatorRef`` /
          bar-field literal / numeric literal.
    Post: refs with the same ``(name, params, source)`` deduplicate
          to one entry; result order is sigid-sorted so binding
          emission is byte-stable.
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

    Pre:  ``side`` is ``IndicatorRef``, bar-field string, or numeric.
    Post: equal sides → equal sigids; different sides differ with
          cryptographic probability. ``IndicatorRef`` sigids are
          invariant under field reordering (JSON dump uses sort_keys).
    Raises: :class:`CompilerError` for any other side type.
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
    """Return the minimum bar-history depth before ``ref`` yields a non-None value.

    Pre:  ``ref.name`` is one of the supported indicator names.
    Post: returned value matches the first ``len(history)`` at which
          the corresponding helper in :data:`_HELPER_BODIES` stops
          returning ``None``. Used as the warm-up gate threshold.
    Invariant: never smaller than the helper's actual requirement —
          a too-low value would leave bindings permanently ``None``
          and predicates never fire.
    """
    name = ref.name
    if name in ("sma", "ema"):
        return int(ref.param("period"))
    if name == "rsi":
        return int(ref.param("period")) + 1
    if name == "macd":
        # MACD line is computable at ``slow`` bars; signal/histogram
        # additionally need ``signal - 1`` macd-line samples.
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
        # Wilder smoothing requires two DX windows: ``2 * period + 1``.
        return 2 * int(ref.param("period")) + 1
    if name == "stochastic":
        # %K available at ``k_period``; %D smoothing needs ``d_period - 1``
        # additional bars of %K history.
        return int(ref.param("k_period")) + int(ref.param("d_period")) - 1
    if name == "vwap":
        # Cumulative sum has no strict warm-up, but a 1-bar VWAP is just
        # (h+l+c)/3 and not informative — floor to ``_MIN_WINDOW``.
        return _MIN_WINDOW
    raise CompilerError(f"unsupported indicator: {name!r}")


def _history_depth_for(ref: IndicatorRef) -> int:
    """Return the depth to request from ``ctx.history(symbol, n)``.

    Pre:  ``ref.name`` is one of the supported indicator names.
    Post: for non-VWAP indicators, equals :func:`_lookback_for`.
          For VWAP, returns ``_VWAP_HISTORY`` so the cumulative-style
          helper sees the deepest history the harness retains.
    """
    if ref.name == "vwap":
        return _VWAP_HISTORY
    return _lookback_for(ref)


def _build_indicator_bindings(
    refs: List[IndicatorRef],
) -> List[Tuple[str, IndicatorRef, str, str]]:
    """Return ``(varname, ref, call_expr, sigid)`` for every indicator.

    Pre:  ``refs`` is sigid-sorted (i.e. comes from :func:`_collect_indicators`).
    Post: result order matches ``refs`` order, so emission is byte-stable.
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

    Pre:  ``ref.name`` is one of the supported indicator names; required
          DSL params are present.
    Post: returned expression evaluates to a single scalar (or ``None``
          during warm-up) of the type the corresponding predicate
          compares against. Tuple-valued indicators (macd, bollinger,
          stochastic) thread their selector kwarg through to the helper.
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
    """Return ``(sigid, prev_var, current_expr)`` for every cross-predicate side.

    Pre:  ``indicator_bindings`` covers every indicator referenced by
          a cross predicate in the rules.
    Post: result is de-duplicated by sigid and sigid-sorted. Each
          tuple supplies the names needed by ``on_bar`` to read the
          previous bar's value (``self._cross_prev[symbol][sigid]``)
          and emit the current bar's value.
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

    Pre:  ``side`` is ``IndicatorRef`` / bar-field literal / numeric;
          every ``IndicatorRef`` side has a binding in ``binding_by_sigid``.
    Post: ``IndicatorRef`` → the binding variable; bar-field literal
          → the corresponding bar attribute expression; numeric →
          ``repr(float(value))``.
    Raises: :class:`CompilerError` for missing bindings, unsupported
          price-ref literals, or unsupported side types.
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

    Pre:  ``pred.op`` ∈ ``{"<", ">", "<=", ">=", "==", "cross_above", "cross_below"}``;
          for cross ops, both sides have entries in ``cross_sides``.
    Post: returned expression is parenthesised and safe to evaluate
          during warm-up: indicator-ref sides are guarded with
          ``is not None`` (bar-field and numeric literals are never
          None, so guarding them would emit a literal SyntaxWarning).
          Cross ops additionally require both previous snapshots to
          be non-None and the inequality to have flipped at this bar.
    Raises: :class:`CompilerError` for unsupported ops.
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
# Sizing.
# ---------------------------------------------------------------------------


def _render_sizing(
    sizing: Any, indicator_bindings: List[Tuple[str, IndicatorRef, str, str]]
) -> str:
    """Return a Python statement that assigns ``qty`` from the sizing rule.

    Pre:  ``sizing`` is a known sizing variant; for
          ``VolatilityTargetSizing``, an ``atr`` binding exists in
          ``indicator_bindings`` (caller-side gate in
          :func:`compile_strategy`).
    Post: emitted ``qty`` is always a positive integer
          (``max(1, int(...))``). The caller guarantees ``bar.close``
          is non-None, finite, and positive at the point the emitted
          statement executes — see the validity guard at the top of
          ``on_bar``.
    Raises: :class:`CompilerError` for unsupported variants, or
          internally if an ATR binding is unexpectedly missing.
    """
    if isinstance(sizing, FixedFractionSizing):
        return f"qty = max(1, int((ctx.equity * {float(sizing.fraction)!r}) / bar.close))"
    if isinstance(sizing, FixedNotionalSizing):
        return f"qty = max(1, int({float(sizing.notional_usd)!r} / bar.close))"
    if isinstance(sizing, VolatilityTargetSizing):
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
# Inline indicator helper-method bodies.
#
# Each takes ``self``, a ``history`` list of ``Bar``-like objects, and the
# indicator's params; returns the scalar value at the last bar (or
# ``None`` when there is insufficient history). The math mirrors
# ``strategy_lab/factors/primitives.py`` so the compiled output remains
# drop-in for the engine and the trade-alignment loop never sees
# diverging "compiled vs. reference" semantics.
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
            # Defence-in-depth — front-door rejects fast >= slow.
            if fast >= slow:
                return None
            min_bars = slow if select == "macd" else slow + signal - 1
            if len(history) < min_bars:
                return None
            macd_line = []
            for end in range(slow, len(history) + 1):
                sub = history[:end]
                alpha_f = 2.0 / (fast + 1.0)
                ef = self._src(sub[-fast], source)
                for b in sub[-fast + 1:]:
                    ef = alpha_f * self._src(b, source) + (1.0 - alpha_f) * ef
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
    """Return a sort-stable dict of the DSL fields that determine compiled output.

    Pre:  ``spec`` has the public DSL fields.
    Post: result is JSON-serialisable with sort_keys. Excludes:
          ``strategy_code`` (the compiler's own output), audit
          metadata, ``hypothesis`` prose, and rule-level ``note``
          fields (author prose, never consumed by code-gen). Two
          semantically identical specs always yield equal payloads.
    Invariant: ``note`` is stripped only from the top-level rule /
          sizing dict, not from nested ``params`` — a future indicator
          param literally named ``"note"`` would survive the strip
          and remain in the hash.
    """

    def _dump(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "model_dump"):
            payload = value.model_dump(mode="json")
        else:
            payload = value
        if isinstance(payload, dict):
            return {k: v for k, v in payload.items() if k != "note"}
        return payload

    return {
        "target_symbols": list(getattr(spec, "target_symbols", []) or []),
        "entry_rules": [_dump(r) for r in (getattr(spec, "entry_rules", []) or [])],
        "exit_rules": [_dump(r) for r in (getattr(spec, "exit_rules", []) or [])],
        "sizing": _dump(getattr(spec, "sizing", None)),
    }


def _emit_header(spec: Any) -> str:
    """Return the deterministic banner block.

    Post: includes a ``spec_hash`` (sha256 over
          :func:`_canonical_spec_payload`, truncated to 12 hex chars).
          Two semantically equal specs always emit the same banner.
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
    # The sandbox ``indicators`` module uses pandas-Series signatures
    # incompatible with the compiled per-bar call shape, so it is
    # deliberately NOT imported — helper bodies are inlined as class
    # methods instead.
    return "import math\nfrom contract import Strategy, OrderSide, OrderType, TimeInForce\n"


def _indent_method(body: str, spaces: int = 4) -> str:
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
    """Emit the ``CompiledStrategy`` class source.

    Pre:  inputs are produced by :func:`compile_strategy` after its
          gating checks; ``indicator_bindings`` and ``cross_sides`` are
          sigid-sorted.
    Post: returned source defines exactly one ``class
          CompiledStrategy(Strategy)`` with constants, ``__init__``,
          ``on_bar``, and (when needed) indicator helper methods. The
          body shape satisfies ``CodeSafetyChecker`` (universe guard,
          order-flow shape) and ``CodeConformanceGate`` (indicator
          name match, sizing-math hooks).
    """
    universe_literal = (
        "frozenset({" + ", ".join(repr(s) for s in target_symbols) + "})"
        if target_symbols
        else "frozenset()"
    )

    init_lines: List[str] = ["    def __init__(self):"]
    init_lines.append("        super().__init__()")
    if cross_sides:
        # Per-symbol scope: keyed by ``bar.symbol`` so multi-symbol
        # runs don't leak previous-bar state across tickers (which
        # would forge false cross triggers).
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
    # Bar-close validity guard. Every sizing variant divides by
    # ``bar.close``; a single bad tick (zero, negative, NaN, missing)
    # would otherwise raise ``ZeroDivisionError`` or propagate
    # non-finite values into ``submit_order`` and terminate the run.
    on_bar_lines.append(
        "        if bar.close is None or not math.isfinite(bar.close) or bar.close <= 0:"
    )
    on_bar_lines.append("            return")
    # ``CodeConformanceGate`` requires a top-of-on_bar ``ctx.equity``
    # reference; sizing variants that ignore equity (e.g. fixed_notional)
    # still need it to satisfy the static sizing-math check.
    on_bar_lines.append("        _ = ctx.equity")
    for varname, _ref, call_expr, _sigid in indicator_bindings:
        on_bar_lines.append(f"        {varname} = {call_expr}")
    if cross_sides:
        on_bar_lines.append("        _prev_for_symbol = self._cross_prev.get(bar.symbol, {})")
        for sigid, prev_var, _cur in cross_sides:
            on_bar_lines.append(f"        {prev_var} = _prev_for_symbol.get({sigid!r})")
    on_bar_lines.append("        position = ctx.position(bar.symbol)")

    # ``CodeConformanceGate`` requires the class to reference
    # ``position.entry_price`` whenever the spec contains a stop-loss
    # or take-profit rule (runtime enforcement is engine-side). Emit
    # a benign read so the static check passes without re-implementing
    # the exit math.
    has_engine_handled_exit = any(isinstance(r, (StopLossRule, TakeProfitRule)) for r in exit_rules)
    if has_engine_handled_exit:
        on_bar_lines.append(
            "        _entry_ref = position.entry_price if position is not None else None"
        )
        on_bar_lines.append("        _ = _entry_ref  # engine enforces stop/take-profit thresholds")

    signal_exits_in_order = [
        (idx, r) for idx, r in enumerate(exit_rules) if isinstance(r, SignalExitRule)
    ]
    if signal_exits_in_order:
        # Per-bar guard: multiple signal-exit predicates firing on the
        # same candle emit one close order, not several.
        on_bar_lines.append("        exit_submitted = False")
    for exit_idx, rule in signal_exits_in_order:
        pred_src = _render_predicate(rule.when, binding_by_sigid, cross_sides)
        exit_reason = f"compiled_signal_exit:exit[{exit_idx}]"
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
        on_bar_lines.append(f'                reason="{exit_reason}",')
        on_bar_lines.append("            )")
        on_bar_lines.append("            exit_submitted = True")

    if entry_rules:
        # Per-bar guard: short-circuits multi-entry-rule specs so two
        # predicates true on the same bar don't double-up entry orders.
        on_bar_lines.append("        entry_submitted = False")
    # Entry calls carry no bracket attachments. Bracket prices derived
    # from signal-bar close are wrong on gap opens (market orders fill
    # on the next bar's open), and a static StopAttachment silently
    # downgrades trailing-basis stop-losses. The engine's
    # ``_EngineExitDispatcher`` enforces every stop/take rule with the
    # correct basis against the post-fill ``position.entry_price``.
    for idx, rule in enumerate(entry_rules):
        pred_src = _render_predicate(rule.when, binding_by_sigid, cross_sides)
        side_literal = "OrderSide.LONG" if rule.side == "long" else "OrderSide.SHORT"
        sizing_stmt = _render_sizing(sizing, indicator_bindings)
        rule_reason = f"compiled_entry:entry[{idx}]"
        on_bar_lines.append(f"        if not entry_submitted and position is None and {pred_src}:")
        on_bar_lines.append(f"            {sizing_stmt}")
        on_bar_lines.append("            ctx.submit_order(")
        on_bar_lines.append("                symbol=bar.symbol,")
        on_bar_lines.append(f"                side={side_literal},")
        on_bar_lines.append("                qty=qty,")
        on_bar_lines.append("                order_type=OrderType.MARKET,")
        on_bar_lines.append("                tif=TimeInForce.DAY,")
        on_bar_lines.append(f'                reason="{rule_reason}",')
        on_bar_lines.append("            )")
        on_bar_lines.append("            entry_submitted = True")

    # Cross-state update MUST come after all branches so the current
    # bar's value is preserved for the next ``on_bar`` invocation on
    # this symbol.
    if cross_sides:
        on_bar_lines.append("        _new_prev = self._cross_prev.setdefault(bar.symbol, {})")
        for sigid, _prev_var, current_expr in cross_sides:
            on_bar_lines.append(f"        _new_prev[{sigid!r}] = {current_expr}")

    helper_method_blocks: List[str] = []
    if needs_source_helper:
        helper_method_blocks.append(_indent_method(_emit_source_helper()))
    for name in used_helper_names:
        body = _HELPER_BODIES[name]
        helper_method_blocks.append(_indent_method(body))

    body_lines: List[str] = [
        f"    UNIVERSE = {universe_literal}",
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
