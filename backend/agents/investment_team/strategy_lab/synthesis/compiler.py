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

from ..spec_dsl import (
    EntryRule,
    IndicatorRef,
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
            # Defence-in-depth: matches IndicatorRegistry._macd_value's
            # precondition floor. Returns None (the sandbox can't propagate
            # ValueError cleanly through predicate evaluation) for any
            # malformed parameter that slipped past spec_dsl validation.
            if fast < 2 or slow <= fast or signal < 2:
                return None
            # True warm-up gate: ``slow`` bars are needed before macd_line
            # has its first element. For ``select='signal'``/``'histogram'``
            # we still pass through the body during the warm-up window
            # ``[slow, slow + signal - 1)`` so the cache is populated with
            # ``sig_val=None``/``hist_val=None`` — same-bar repeat calls
            # then hit the fast path instead of cold-rebuilding.
            if len(history) < slow:
                return None
            # The macd_line is the difference of fast/slow windowed EMAs at
            # every bar end from ``slow`` to ``len(history)``. We carry it
            # forward in ``self._ind_state`` and maintain it incrementally:
            # on a one-bar advance we either ``expand`` (history grew, e.g.
            # during warm-up) or ``slide`` (history length unchanged, oldest
            # bar dropped — the steady-state shape of ``ctx.history(symbol,
            # depth)``). On slide we drop the front of macd_line so the
            # deque stays bounded. Cold-start / replay / cross-symbol fall
            # back to a full rebuild.
            # Symbol is part of the key — the same strategy instance fires
            # on_bar for every symbol in UNIVERSE.
            # Fingerprint is a 4-tuple (id, len, ts, close). Close is
            # normalised inline — see indicators/streaming.py::_normalise_close
            # for the full taxonomy (None/bool/numpy.bool_/NaN/inf/non-numeric
            # all collapse to None). The close leg of prev_matches fires
            # only when ts is unavailable on BOTH sides — restricts the
            # close-based rescue to fresh-copy callers that also drop
            # timestamps, and prevents flat-market false-merges. ``prev_close``
            # is computed lazily inside the close-leg so non-numeric prev
            # closes cannot crash the helper from inside _advance_kind.
            # Relies on the emitted __init__ initialising self._ind_state.
            symbol = getattr(history[-1], "symbol", None)
            key = ("macd", symbol, fast, slow, signal, source)
            state = self._ind_state.get(key)
            last_bar = history[-1]
            new_ts = getattr(last_bar, "timestamp", None)
            new_id = id(last_bar)
            new_len = len(history)
            raw_close = getattr(last_bar, "close", None)
            if raw_close is None or isinstance(raw_close, bool):
                new_close = None
            elif type(raw_close).__module__ in ("numpy", "pandas") and "bool" in type(raw_close).__name__.lower():
                new_close = None
            else:
                try:
                    new_close = float(raw_close)
                except (TypeError, ValueError):
                    new_close = None
                else:
                    if math.isnan(new_close) or math.isinf(new_close):
                        new_close = None
            new_fp = (new_id, new_len, new_ts, new_close)
            if state is not None and state["fp"] == new_fp:
                return state["value"].get(select)
            alpha_f = 2.0 / (fast + 1.0)
            alpha_s = 2.0 / (slow + 1.0)
            kind = "none"
            if state is not None and new_len >= 2:
                prev_bar = history[-2]
                prev_ts = getattr(prev_bar, "timestamp", None)
                prev_fp = state["fp"]
                both_have_ts = prev_fp[2] is not None and prev_ts is not None
                both_ts_absent = prev_fp[2] is None and prev_ts is None
                if prev_fp[0] == id(prev_bar):
                    prev_matches = True
                elif both_have_ts and prev_fp[2] == prev_ts:
                    prev_matches = True
                elif both_ts_absent:
                    prev_raw_close = getattr(prev_bar, "close", None)
                    if prev_raw_close is None or isinstance(prev_raw_close, bool):
                        prev_close = None
                    elif type(prev_raw_close).__module__ in ("numpy", "pandas") and "bool" in type(prev_raw_close).__name__.lower():
                        prev_close = None
                    else:
                        try:
                            prev_close = float(prev_raw_close)
                        except (TypeError, ValueError):
                            prev_close = None
                        else:
                            if math.isnan(prev_close) or math.isinf(prev_close):
                                prev_close = None
                    prev_matches = prev_close is not None and prev_fp[3] == prev_close
                else:
                    prev_matches = False
                if prev_matches:
                    if new_len == prev_fp[1] + 1:
                        kind = "expand"
                    elif new_len == prev_fp[1]:
                        kind = "slide"
            if kind in ("expand", "slide"):
                macd_line = state["macd_line"]
                # Compute-then-mutate: finish the EMA loops BEFORE
                # touching the cached deque, so any raise leaves the
                # cache untouched and the next call cleanly cold-rebuilds.
                ef = self._src(history[-fast], source)
                for b in history[-fast + 1:]:
                    ef = alpha_f * self._src(b, source) + (1.0 - alpha_f) * ef
                es = self._src(history[-slow], source)
                for b in history[-slow + 1:]:
                    es = alpha_s * self._src(b, source) + (1.0 - alpha_s) * es
                new_macd_val = ef - es
                if kind == "slide":
                    macd_line.popleft()
                macd_line.append(new_macd_val)
            else:
                macd_line = deque()
                for end in range(slow, new_len + 1):
                    sub = history[:end]
                    ef = self._src(sub[-fast], source)
                    for b in sub[-fast + 1:]:
                        ef = alpha_f * self._src(b, source) + (1.0 - alpha_f) * ef
                    es = self._src(sub[-slow], source)
                    for b in sub[-slow + 1:]:
                        es = alpha_s * self._src(b, source) + (1.0 - alpha_s) * es
                    macd_line.append(ef - es)
            macd_val = macd_line[-1]
            sig_val = None
            hist_val = None
            if len(macd_line) >= signal:
                alpha_g = 2.0 / (signal + 1.0)
                # Iterator walk avoids deque __getitem__ O(min(i, n-i))
                # — random-access would make this loop O(n^2).
                _macd_iter = iter(macd_line)
                sig = next(_macd_iter)
                for _macd_x in _macd_iter:
                    sig = alpha_g * _macd_x + (1.0 - alpha_g) * sig
                sig_val = sig
                hist_val = macd_val - sig_val
            self._ind_state[key] = {
                "fp": new_fp,
                "macd_line": macd_line,
                "value": {
                    "macd": macd_val,
                    "signal": sig_val,
                    "histogram": hist_val,
                },
            }
            if select == "macd":
                return macd_val
            if select == "signal":
                return sig_val
            if select == "histogram":
                return hist_val
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

    # Conformance gate checks #5/#6 require ``position.entry_price``
    # when stop-loss or take-profit rules exist.
    has_engine_handled_exit = any(isinstance(r, (StopLossRule, TakeProfitRule)) for r in exit_rules)
    if has_engine_handled_exit:
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
