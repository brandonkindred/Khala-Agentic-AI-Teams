"""Deterministic ``StrategySpec`` → canonical Python compiler.

``compile_strategy(spec)`` turns a structured ``StrategySpec`` into the
``Strategy`` subclass the streaming harness expects.

Module contract:
  Determinism: the same spec always produces byte-identical output.
    The header carries a SHA-256 content hash of a canonical DSL dump
    for traceability; no other source of variation is admitted (no
    ``datetime.now()``, no ``uuid``, no ``id()``).
  Engine-only ordering: the compiled ``on_bar`` is a thin shim —
    universe guard, warm-up gate, bar-close validity, indicator
    computation, and a position/entry_price read for conformance
    checks. **No entry or exit orders are submitted from the compiled
    code.** All entry decisions are made by ``_EngineEntryDispatcher``
    (via ``evaluate_entry_rules``), all exit decisions by
    ``_EngineExitDispatcher`` (via ``evaluate_exit_rules``), both
    using the shared ``predicate_evaluator`` module. This eliminates
    the dual-path conflict where strategy-side and engine-side
    predicate evaluation could diverge.
  Indicator math: helper bodies are inlined as class methods, not
    imported, because the sandbox's ``indicators`` module uses
    pandas-Series signatures incompatible with the per-bar call shape.
    Method names match ``CodeConformanceGate._INDICATOR_ALLOWED_CALL_NAMES``.
  Tuple indicators: MACD / Bollinger / Stochastic thread the DSL's
    ``output`` / ``band`` selector into the helper and return a scalar.
  ``volatility_target`` sizing requires an ``atr`` indicator in the
    spec; absent or ambiguous ATR raises :class:`CompilerError`.
  Empty ``spec.target_symbols`` is supported (no universe guard emitted).
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from typing import Any, List, Tuple

from ..indicators.registry_metadata import EMIT_ARGS as _EMIT_ARGS
from ..indicators.registry_metadata import (
    INDICATOR_METADATA,
    MIN_WINDOW,
    lookback_for,
)
from ..indicators.template_bodies import render_adx_body, render_macd_body, render_vwap_body
from ..runtime_window import STREAMING_WINDOW_BARS
from ..spec_dsl import (
    _INDICATOR_PARAM_SPECS,
    INDICATOR_HELPER_NAME,
    EntryRule,
    IndicatorName,
    IndicatorRef,
    SignalExitRule,
    VolatilityTargetSizing,
    is_entry_anchored_exit,
    iter_tree_indicator_refs,
)

# Indicators that accept a ``source`` override, derived from the DSL param specs
# so the emitted ``_src`` helper is requested for exactly the source-aware
# indicators — no hand-maintained tuple to drift from ``allow_source``.
_SOURCE_AWARE_NAMES: frozenset[str] = frozenset(
    name for name, spec in _INDICATOR_PARAM_SPECS.items() if spec.get("allow_source")
)


class CompilerError(Exception):
    """Raised when a spec cannot be expressed by the deterministic compiler.

    The orchestrator treats this as a signal to fall back to LLM-authored
    code: it sets ``spec.requires_custom_code = True`` and retains the
    ideation-generated ``strategy_code`` instead of the compiled output.
    """


# DSL name → emitted method name, derived from the single source of truth in
# ``spec_dsl.INDICATOR_HELPER_NAME`` (which carries the load-time coverage guard).
# The conformance gate credits ``self.<name>(...)`` off the same mapping.
_INDICATOR_METHOD_NAME: dict[str, str] = dict(INDICATOR_HELPER_NAME)

# Single source: ``indicators.registry_metadata.MIN_WINDOW``.
_MIN_WINDOW: int = MIN_WINDOW

# OBV requests the deepest retained history — it's cumulative-over-the-series,
# not rolling, so a smaller request would silently change signal semantics;
# this mirrors the harness retention ceiling in ``StrategyContext._ingest_bar``
# via the shared constant. VWAP no longer needs this: it now takes a rolling
# ``period`` (unified with the factors DSL's VWAP, which has always been
# rolling), so its history request is just its own lookback like every other
# windowed indicator.
_OBV_HISTORY: int = STREAMING_WINDOW_BARS


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
    used_helper_names = sorted({_INDICATOR_METHOD_NAME[ref.name] for ref in indicator_refs})
    needs_source_helper = any(ref.name in _SOURCE_AWARE_NAMES for ref in indicator_refs)

    # ``history_depth`` (request) and ``warmup_min`` (gate) are decoupled
    # so VWAP's cumulative-style depth request doesn't bind the warm-up
    # threshold of every other indicator to 500 bars.
    history_depth = max((_history_depth_for(ref) for ref in indicator_refs), default=_MIN_WINDOW)
    history_depth = max(history_depth, _MIN_WINDOW)
    warmup_min = max((_lookback_for(ref) for ref in indicator_refs), default=_MIN_WINDOW)
    warmup_min = max(warmup_min, _MIN_WINDOW)

    parts: List[str] = []
    parts.append(_emit_header(spec))
    # ``deque`` is only needed by the MACD helper's cached macd_line.
    parts.append(_emit_imports(needs_deque="macd" in used_helper_names))
    parts.append(
        _emit_class(
            target_symbols=target_symbols,
            history_depth=history_depth,
            warmup_min=warmup_min,
            indicator_bindings=indicator_bindings,
            exit_rules=exit_rules,
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
        for side in iter_tree_indicator_refs(rule.when):
            sigid = _sigid_for_side(side)
            seen.setdefault(sigid, side)
    for rule in signal_exit_rules:
        for side in iter_tree_indicator_refs(rule.when):
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

    Delegates to ``indicators.registry_metadata.lookback_for`` — the single
    source of truth for every indicator's lookback formula, also consulted
    by ``factors.compiler._lookback`` for the indicators the two DSLs share.
    """
    if ref.name not in INDICATOR_METADATA:
        raise CompilerError(f"unsupported indicator: {ref.name!r}")
    return lookback_for(ref.name, ref.params)


def _history_depth_for(ref: IndicatorRef) -> int:
    """Return the depth to request from ``ctx.history(symbol, n)``.

    Pre:  ``ref.name`` is one of the supported indicator names.
    Post: for non-cumulative indicators (which, since VWAP's rolling-window
          unification, is every indicator but OBV), equals
          :func:`_lookback_for`. OBV is still cumulative-over-the-series, so
          it returns ``_OBV_HISTORY`` so the helper sees the deepest history
          the harness retains.
    """
    if ref.name == "obv":
        return _OBV_HISTORY
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


# Per-indicator emit spec: the ordered kwargs to thread into ``self.<method>(history, …)``.
# Each entry is ``(emit_kwarg, kind, dsl_param)`` where ``kind`` is:
#   "int"    → ``{emit_kwarg}={int(ref.param(dsl_param))}``
#   "float"  → ``{emit_kwarg}={float(ref.param(dsl_param))!r}``
#   "source" → ``{emit_kwarg}={ref.source!r}``            (dsl_param unused)
#   "select" → ``{emit_kwarg}={str(ref.param(dsl_param))!r}``  (selector for tuple-valued indicators)
# Imported above as ``_EMIT_ARGS`` from ``indicators.registry_metadata.EMIT_ARGS`` —
# the single source of truth shared with the per-indicator descriptor table.

# Load-time guard: the emit table must cover exactly the DSL indicator names, or a
# valid ref would ``KeyError`` at emit time. Mirrors the ``_INDICATOR_METHOD_NAME`` guard.
if set(_EMIT_ARGS) != set(IndicatorName.__args__):
    raise RuntimeError(
        "indicator emit table (_EMIT_ARGS) must cover every DSL indicator; "
        f"mismatch: {set(IndicatorName.__args__) ^ set(_EMIT_ARGS)}"
    )


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
    args = ["history"]
    for emit_kwarg, kind, dsl_param in _EMIT_ARGS[ref.name]:
        if kind == "int":
            args.append(f"{emit_kwarg}={int(ref.param(dsl_param))}")
        elif kind == "float":
            args.append(f"{emit_kwarg}={float(ref.param(dsl_param))!r}")
        elif kind == "source":
            args.append(f"{emit_kwarg}={ref.source!r}")
        else:  # "select"
            args.append(f"{emit_kwarg}={str(ref.param(dsl_param))!r}")
    return f"self.{method}({', '.join(args)})"


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
        def _src(self, bar: object, source: str) -> float:
            if source == "close":
                return bar.close
            if source == "high":
                return bar.high
            if source == "low":
                return bar.low
            if source == "open":
                return bar.open
            if source == "volume":
                # Canonicalise a non-finite volume to 0.0 (the sentinel the data
                # boundaries and dataset fingerprint use) so a NaN/inf volume
                # can't make a predicate behave differently from the 0.0 dataset
                # it shares a fingerprint with. Self-contained: v == v is False
                # only for NaN.
                v = bar.volume
                return v if v == v and v != float("inf") and v != float("-inf") else 0.0
            if source == "hl2":
                return (bar.high + bar.low) / 2.0
            if source == "ohlc4":
                return (bar.open + bar.high + bar.low + bar.close) / 4.0
            return bar.close
        """
    )


# MACD and ADX are built from the canonical shared bodies in
# ``indicators.template_bodies`` (also consumed by ``factors.compiler``) so
# the ~200-line streaming-cache classification and the directional-movement
# loop each have exactly one source. ``{fast}``/``{slow}``/``{signal}``/
# ``{period}`` are burned into their own bound parameter names here (unlike
# ``factors.compiler``, which defers that ``.format()`` to per-node
# compile time with int literals).
_MACD_HELPER_BODY: str = render_macd_body(
    bars_var="history",
    missing="None",
    cache_key_expr='("macd", _symbol, {fast}, {slow}, {signal}, source)',
    select_expr="select",
    close_expr_template="self._src({obj}, source)",
).format(fast="fast", slow="slow", signal="signal")

_ADX_HELPER_BODY: str = render_adx_body(bars_var="history", missing="None").format(period="period")

_VWAP_HELPER_BODY: str = render_vwap_body(bars_var="history", missing="None").format(
    period="period"
)

_HELPER_BODIES: dict[str, str] = {
    "sma": textwrap.dedent(
        """\
        def sma(self, history: list, period: int, source: str = "close") -> float | None:
            if len(history) < period:
                return None
            vals = [self._src(b, source) for b in history[-period:]]
            return sum(vals) / period
        """
    ),
    "ema": textwrap.dedent(
        """\
        def ema(self, history: list, period: int, source: str = "close") -> float | None:
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
        def rsi(self, history: list, period: int = 14, source: str = "close") -> float | None:
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
    "macd": (
        "def macd(self, history: list, fast: int = 12, slow: int = 26, signal: int = 9, "
        'source: str = "close", select: str = "macd") -> float | None:\n'
        + textwrap.indent(_MACD_HELPER_BODY, "    ")
    ),
    "bollinger_bands": textwrap.dedent(
        """\
        def bollinger_bands(self, history: list, period: int = 20, num_std: float = 2.0, source: str = "close", select: str = "middle") -> float | None:
            if len(history) < period:
                return None
            s = sq = 0.0
            for b in history[-period:]:
                v = self._src(b, source)
                s += v
                sq += v * v
            mean = s / period
            var = max(0.0, sq / period - mean * mean)
            std = math.sqrt(var) if var > 0 else 0.0
            upper = mean + num_std * std
            lower = mean - num_std * std
            if select == "middle":
                return mean
            if select == "upper":
                return upper
            if select == "lower":
                return lower
            if select == "percent_b":
                width = upper - lower
                if width == 0:
                    return 0.5
                price = self._src(history[-1], source)
                return (price - lower) / width
            if select == "bandwidth":
                if mean == 0:
                    return 0.0
                return (upper - lower) / mean
            return None
        """
    ),
    "atr": textwrap.dedent(
        """\
        def atr(self, history: list, period: int = 14) -> float | None:
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
    "adx": (
        "def adx(self, history: list, period: int = 14) -> float | None:\n"
        + textwrap.indent(_ADX_HELPER_BODY, "    ")
    ),
    "stochastic": textwrap.dedent(
        """\
        def stochastic(self, history: list, k_period: int = 14, d_period: int = 3, select: str = "k") -> float | None:
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
            k_vals = [_k_at(end) for end in range(len(history) - d_period + 1, len(history) + 1)]
            return sum(k_vals) / d_period
        """
    ),
    "vwap": (
        "def vwap(self, history: list, period: int = 20) -> float | None:\n"
        + textwrap.indent(_VWAP_HELPER_BODY, "    ")
    ),
    "donchian_channels": textwrap.dedent(
        """\
        def donchian_channels(self, history: list, period: int = 20, select: str = "middle") -> float | None:
            if len(history) < period:
                return None
            window = history[-period:]
            upper = max(b.high for b in window)
            lower = min(b.low for b in window)
            if select == "upper":
                return upper
            if select == "lower":
                return lower
            if select == "middle":
                return (upper + lower) / 2.0
            return None
        """
    ),
    "keltner_channels": textwrap.dedent(
        """\
        def keltner_channels(self, history: list, period: int = 20, atr_period: int = 10, multiplier: float = 2.0, select: str = "middle") -> float | None:
            if len(history) < max(period, atr_period + 1):
                return None
            alpha = 2.0 / (period + 1.0)
            middle = history[-period].close
            for b in history[-period + 1:]:
                middle = alpha * b.close + (1.0 - alpha) * middle
            # Simple average of true range (matches IndicatorRegistry.atr and
            # the streaming keltner — atr is an SMA of TR, not Wilder-smoothed).
            total = 0.0
            for i in range(len(history) - atr_period, len(history)):
                h = history[i].high
                low = history[i].low
                prev_close = history[i - 1].close
                total += max(h - low, abs(h - prev_close), abs(low - prev_close))
            atr_val = total / atr_period
            if select == "middle":
                return middle
            if select == "upper":
                return middle + multiplier * atr_val
            if select == "lower":
                return middle - multiplier * atr_val
            return None
        """
    ),
    "obv": textwrap.dedent(
        """\
        def obv(self, history: list) -> float | None:
            if not history:
                return None
            value = 0.0
            for i in range(1, len(history)):
                cur = history[i].close
                prev = history[i - 1].close
                if cur > prev:
                    value += history[i].volume
                elif cur < prev:
                    value -= history[i].volume
            return value
        """
    ),
    "mfi": textwrap.dedent(
        """\
        def mfi(self, history: list, period: int = 14) -> float | None:
            if len(history) < period + 1:
                return None
            pos = 0.0
            neg = 0.0
            for i in range(len(history) - period, len(history)):
                cur = history[i]
                prev = history[i - 1]
                tp = (cur.high + cur.low + cur.close) / 3.0
                tp_prev = (prev.high + prev.low + prev.close) / 3.0
                rmf = tp * cur.volume
                if tp > tp_prev:
                    pos += rmf
                elif tp < tp_prev:
                    neg += rmf
            if neg == 0:
                return 100.0 if pos > 0 else 50.0
            ratio = pos / neg
            return 100.0 - (100.0 / (1.0 + ratio))
        """
    ),
    "roc": textwrap.dedent(
        """\
        def roc(self, history: list, period: int = 12, source: str = "close") -> float | None:
            if len(history) < period + 1:
                return None
            cur = self._src(history[-1], source)
            prev = self._src(history[-1 - period], source)
            if prev == 0:
                return 0.0
            return (cur - prev) / prev * 100.0
        """
    ),
    "cci": textwrap.dedent(
        """\
        def cci(self, history: list, period: int = 20) -> float | None:
            if len(history) < period:
                return None
            tps = [(b.high + b.low + b.close) / 3.0 for b in history[-period:]]
            sma_tp = sum(tps) / period
            mean_dev = sum(abs(t - sma_tp) for t in tps) / period
            if mean_dev == 0:
                return 0.0
            return (tps[-1] - sma_tp) / (0.015 * mean_dev)
        """
    ),
    "williams_r": textwrap.dedent(
        """\
        def williams_r(self, history: list, period: int = 14) -> float | None:
            if len(history) < period:
                return None
            window = history[-period:]
            highest = max(b.high for b in window)
            lowest = min(b.low for b in window)
            rng = highest - lowest
            if rng == 0:
                return -50.0
            return -100.0 * (highest - history[-1].close) / rng
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


def _emit_imports(*, needs_deque: bool = False) -> str:
    # The sandbox ``indicators`` module uses pandas-Series signatures
    # incompatible with the compiled per-bar call shape, so it is
    # deliberately NOT imported — helper bodies are inlined as class
    # methods instead.
    # No ``OrderSide`` / ``OrderType`` / ``TimeInForce`` — the compiled
    # shim emits zero ``submit_order`` calls; all orders come from the
    # engine dispatchers.
    # ``deque`` backs the MACD helper's cached ``macd_line``; popleft is
    # O(1) where ``list.pop(0)`` would be O(N) and dominates the per-bar
    # cost the cache exists to amortise. Emitted only when the strategy
    # uses MACD — non-MACD specs get a cleaner import surface and any
    # future linter run on emitted modules won't flag F401.
    lines = ["import math"]
    if needs_deque:
        lines.append("from collections import deque")
    lines.append("from contract import Strategy")
    return "\n".join(lines) + "\n"


def _indent_method(body: str, spaces: int = 4) -> str:
    return textwrap.indent(body.rstrip() + "\n", " " * spaces)


def _emit_class(
    *,
    target_symbols: List[str],
    history_depth: int,
    warmup_min: int,
    indicator_bindings: List[Tuple[str, IndicatorRef, str, str]],
    exit_rules: List[Any],
    used_helper_names: List[str],
    needs_source_helper: bool,
) -> str:
    """Emit the ``CompiledStrategy`` class source.

    Pre:  inputs are produced by :func:`compile_strategy` after its
          gating checks; ``indicator_bindings`` are sigid-sorted.
    Post: returned source defines exactly one ``class
          CompiledStrategy(Strategy)`` with constants, ``__init__``,
          ``on_bar``, and (when needed) indicator helper methods. The
          ``on_bar`` is a thin shim: universe guard, warm-up gate,
          bar-close validity, indicator binds, and a position/entry_price
          read for conformance checks. No ``ctx.submit_order`` calls are
          emitted — all entries and exits are handled engine-side by
          ``_EngineEntryDispatcher`` and ``_EngineExitDispatcher``.
    """
    universe_literal = (
        "frozenset({" + ", ".join(repr(s) for s in target_symbols) + "})"
        if target_symbols
        else "frozenset()"
    )

    init_lines: List[str] = ["    def __init__(self):"]
    init_lines.append("        super().__init__()")
    # Persistent per-indicator state for streaming-recurrence helpers
    # (currently used by macd to amortise its per-bar work to O(slow)).
    init_lines.append("        self._ind_state = {}")

    on_bar_lines: List[str] = ["    def on_bar(self, ctx, bar):"]
    on_bar_lines.append("        if ctx.is_warmup:")
    on_bar_lines.append("            return")
    if target_symbols:
        on_bar_lines.append("        if bar.symbol not in self.UNIVERSE:")
        on_bar_lines.append("            return")
    on_bar_lines.append(f"        history = ctx.history(bar.symbol, {history_depth})")
    on_bar_lines.append(f"        if len(history) < {warmup_min}:")
    on_bar_lines.append("            return")
    on_bar_lines.append(
        "        if bar.close is None or not math.isfinite(bar.close) or bar.close <= 0:"
    )
    on_bar_lines.append("            return")
    for varname, _ref, call_expr, _sigid in indicator_bindings:
        on_bar_lines.append(f"        {varname} = {call_expr}")
    on_bar_lines.append("        position = ctx.position(bar.symbol)")

    # The conformance gate's stop-loss / take-profit enforcement checks require
    # the compiled class to reference ``position.entry_price`` whenever an
    # entry-anchored exit (stop-loss / take-profit / scaled take-profit) exists.
    has_entry_anchored_exit = any(is_entry_anchored_exit(r) for r in exit_rules)
    if has_entry_anchored_exit:
        on_bar_lines.append(
            "        _entry_ref = position.entry_price if position is not None else None"
        )
        on_bar_lines.append("        _ = _entry_ref  # engine enforces stop/take-profit thresholds")

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
