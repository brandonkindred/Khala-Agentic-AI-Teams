"""Generic synthetic-bar helpers shared by the predicate-conformance gate.

These four utilities are pure data plumbing — OHLC clamping, DataFrame
conversion, and universe-symbol resolution — with no rule-fabrication logic.
They were extracted from the now-retired ``rule_probes`` synthesizer so the
surviving :class:`PredicateConformanceGate` (which still drives custom code
through synthetic fixtures to compare against the engine's predicate
evaluator) keeps a stable home for them.

Contract: every function is deterministic and side-effect-free.
"""

from __future__ import annotations

import ast
import math
from typing import Any, List

import pandas as pd

from ...market_data_service import OHLCVBar
from .code_safety_ast import parse_strategy_source

# Floor for synthesised prices. The market-data preflight rejects any bar with
# an OHLC value <= 0 (``_has_nan_or_negative_price``), so we clamp here to keep
# fixtures simple — none of the conformance assertions care about absolute
# price levels, only relative motion.
_MIN_PRICE = 0.01

# Sentinel symbol used when neither ``spec.target_symbols`` nor a compiled
# ``UNIVERSE`` literal names a concrete symbol. Only safe when the compiled
# code carries no universe guard.
_PROBE_SYMBOL_FALLBACK = "PROBE"


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


def _resolve_probe_symbol(spec: Any, compiled_code: str) -> str:
    """Pick a synthetic-bar symbol that matches the compiled code's universe.

    Order of preference:
      1. ``spec.target_symbols[0]`` if non-empty.
      2. An element of the top-level ``UNIVERSE = frozenset({...})`` literal
         parsed out of ``compiled_code`` (if present and non-empty).
      3. The sentinel ``"PROBE"`` — only safe when the compiled code's
         universe filter is empty (i.e. no ``UNIVERSE`` reference in
         ``on_bar``); fixtures that emit this sentinel and find a non-empty
         ``UNIVERSE`` literal mark themselves unsynthesizable downstream.
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


__all__ = [
    "_MIN_PRICE",
    "_PROBE_SYMBOL_FALLBACK",
    "_normalise_ohlc",
    "_bars_to_df",
    "_resolve_probe_symbol",
    "_extract_universe_literal",
]
