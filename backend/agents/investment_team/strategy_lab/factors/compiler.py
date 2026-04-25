"""Deterministic compiler from a typed :class:`Genome` to a sandbox-ready
``contract.Strategy`` Python module string (issue #249, Phase A).

Design
------

* Each unique sub-tree (keyed by canonical-JSON hash) becomes one helper
  method on the generated class.  The same node referenced twice in the
  genome (e.g. SMA(20) appearing in both entry and exit) compiles to a
  single helper.
* Every helper accepts a ``bars`` list and returns its value AT THE LAST
  bar in that list.  Cross-detection helpers call the inner helper twice:
  once with the full ``bars`` and once with ``bars[:-1]`` for the
  previous bar's value.  This keeps the contract uniform and side-effect-
  free; no caching is required because each ``on_bar`` call is short.
* The generated module imports only sandbox-whitelisted symbols
  (``contract`` for ``OrderSide``/``OrderType``/``Strategy`` and ``math``
  for NaN handling).  Pandas / NumPy are deliberately not used.

The compiler does *not* depend on :mod:`primitives` at runtime — the
sandbox's import whitelist excludes the factors package.  Templates here
are checked against ``primitives.py`` by tests in ``test_factor_dsl.py``
and ``test_genome_compiler.py``.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel

from .models import (
    ADX,
    ATR,
    EMA,
    RSI,
    SMA,
    VWAP,
    ATRBreakout,
    BollingerZ,
    BoolAnd,
    BoolNot,
    BoolOr,
    CompareGT,
    CompareLT,
    Const,
    CrossOver,
    CrossUnder,
    FixedQty,
    FundingRateDeviation,
    Genome,
    IfRegime,
    MACDSignal,
    MomentumK,
    PctOfEquity,
    Price,
    Skew,
    StochasticK,
    TermStructureSlope,
    VolRegimeState,
    VolTargeted,
    WeightedSum,
    ZScoreResidualOLS,
)


def _node_id(node: BaseModel) -> str:
    """Stable 8-hex-char identifier for a node based on its canonical JSON form."""
    payload = json.dumps(node.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def _lookback(node: BaseModel) -> int:
    """Approximate minimum bars of history required to evaluate ``node``."""
    if isinstance(node, (Price, Const)):
        return 1
    if isinstance(node, (SMA, EMA, BollingerZ, StochasticK, VWAP)):
        return node.period
    if isinstance(node, RSI):
        return node.period + 1
    if isinstance(node, MACDSignal):
        return node.slow + node.signal
    if isinstance(node, ATR):
        return node.period + 1
    if isinstance(node, ADX):
        return 2 * node.period + 1
    if isinstance(node, MomentumK):
        return node.k + 1
    if isinstance(node, ZScoreResidualOLS):
        return node.window
    if isinstance(node, Skew):
        return node.window + 1
    if isinstance(node, VolRegimeState):
        return node.lookback + 1
    if isinstance(node, (TermStructureSlope, FundingRateDeviation)):
        return 1
    if isinstance(node, WeightedSum):
        return max(_lookback(c) for c in node.children)
    if isinstance(node, IfRegime):
        return max(_lookback(node.gate), _lookback(node.if_true), _lookback(node.if_false))
    if isinstance(node, (CompareGT, CompareLT)):
        return max(_lookback(node.left), _lookback(node.right))
    if isinstance(node, (CrossOver, CrossUnder)):
        return max(_lookback(node.fast), _lookback(node.slow)) + 1
    if isinstance(node, ATRBreakout):
        return max(node.k + 1, node.atr_period + 1)
    if isinstance(node, (BoolAnd, BoolOr)):
        return max(_lookback(c) for c in node.children)
    if isinstance(node, BoolNot):
        return _lookback(node.child)
    raise TypeError(f"Unknown node type for lookback: {type(node).__name__}")


# ---------------------------------------------------------------------------
# Per-node-type method-body templates.  Each template references children by
# their compiled method name; the compiler resolves child IDs before substitution.
# ---------------------------------------------------------------------------


_NUMERIC_TEMPLATES: Dict[type, str] = {
    Price: """\
        if not bars:
            return NAN
        return float(bars[-1].{field})
""",
    Const: """\
        return {value!r}
""",
    SMA: """\
        if len(bars) < {period}:
            return NAN
        return sum(b.close for b in bars[-{period}:]) / {period}
""",
    EMA: """\
        if len(bars) < {period}:
            return NAN
        alpha = 2.0 / ({period} + 1)
        val = bars[-{period}].close
        for _b in bars[-{period} + 1:]:
            val = alpha * _b.close + (1 - alpha) * val
        return val
""",
    RSI: """\
        if len(bars) < {period} + 1:
            return NAN
        gains = 0.0
        losses = 0.0
        for _i in range(len(bars) - {period}, len(bars)):
            _delta = bars[_i].close - bars[_i - 1].close
            if _delta > 0:
                gains += _delta
            else:
                losses += -_delta
        avg_gain = gains / {period}
        avg_loss = losses / {period}
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
""",
    MACDSignal: """\
        if len(bars) < {slow} + {signal}:
            return NAN
        macd_line = []
        for _end in range({slow}, len(bars) + 1):
            _sub = bars[:_end]
            _alpha_f = 2.0 / ({fast} + 1)
            _ef = _sub[-{fast}].close
            for _b in _sub[-{fast} + 1:]:
                _ef = _alpha_f * _b.close + (1 - _alpha_f) * _ef
            _alpha_s = 2.0 / ({slow} + 1)
            _es = _sub[-{slow}].close
            for _b in _sub[-{slow} + 1:]:
                _es = _alpha_s * _b.close + (1 - _alpha_s) * _es
            macd_line.append(_ef - _es)
        if len(macd_line) < {signal}:
            return NAN
        _alpha_g = 2.0 / ({signal} + 1)
        val = macd_line[0]
        for _x in macd_line[1:]:
            val = _alpha_g * _x + (1 - _alpha_g) * val
        return val
""",
    BollingerZ: """\
        if len(bars) < {period}:
            return NAN
        _window = [b.close for b in bars[-{period}:]]
        _mean = sum(_window) / {period}
        _var = sum((x - _mean) ** 2 for x in _window) / {period}
        if _var <= 0:
            return 0.0
        return (bars[-1].close - _mean) / math.sqrt(_var)
""",
    ATR: """\
        if len(bars) < {period} + 1:
            return NAN
        _trs = []
        for _i in range(len(bars) - {period}, len(bars)):
            _h = bars[_i].high
            _l = bars[_i].low
            _pc = bars[_i - 1].close
            _trs.append(max(_h - _l, abs(_h - _pc), abs(_l - _pc)))
        return sum(_trs) / {period}
""",
    ADX: """\
        if len(bars) < 2 * {period} + 1:
            return NAN
        _plus = []
        _minus = []
        _trs = []
        for _i in range(1, len(bars)):
            _up = bars[_i].high - bars[_i - 1].high
            _dn = bars[_i - 1].low - bars[_i].low
            _plus.append(_up if _up > _dn and _up > 0 else 0.0)
            _minus.append(_dn if _dn > _up and _dn > 0 else 0.0)
            _pc = bars[_i - 1].close
            _trs.append(max(
                bars[_i].high - bars[_i].low,
                abs(bars[_i].high - _pc),
                abs(bars[_i].low - _pc),
            ))
        _tr_sum = sum(_trs[-{period}:])
        if _tr_sum == 0:
            return 0.0
        _plus_di = 100.0 * sum(_plus[-{period}:]) / _tr_sum
        _minus_di = 100.0 * sum(_minus[-{period}:]) / _tr_sum
        if _plus_di + _minus_di == 0:
            return 0.0
        return 100.0 * abs(_plus_di - _minus_di) / (_plus_di + _minus_di)
""",
    StochasticK: """\
        if len(bars) < {period}:
            return NAN
        _w = bars[-{period}:]
        _lo = min(b.low for b in _w)
        _hi = max(b.high for b in _w)
        _rng = _hi - _lo
        if _rng == 0:
            return 50.0
        return 100.0 * (bars[-1].close - _lo) / _rng
""",
    VWAP: """\
        if len(bars) < {period}:
            return NAN
        _w = bars[-{period}:]
        _num = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in _w)
        _den = sum(b.volume for b in _w)
        if _den == 0:
            return sum(b.close for b in _w) / {period}
        return _num / _den
""",
    MomentumK: """\
        if len(bars) < {k} + 1:
            return NAN
        _ret = math.log(bars[-1].close / bars[-1 - {k}].close)
        _rets = [
            math.log(bars[_i].close / bars[_i - 1].close)
            for _i in range(len(bars) - {k}, len(bars))
        ]
        if len(_rets) < 2:
            return 0.0
        _m = sum(_rets) / len(_rets)
        _v = sum((r - _m) ** 2 for r in _rets) / len(_rets)
        if _v <= 0:
            return 0.0
        return _ret / math.sqrt(_v * {k})
""",
    ZScoreResidualOLS: """\
        # Cross-symbol residual z-score requires an aligned secondary series.
        # The aux feed is not yet wired (issue #249 follow-up); return NaN so
        # genomes referencing this primitive remain syntactically valid but
        # don't fire signals.  When the feed lands, swap NaN for the OLS calc.
        return NAN
""",
    Skew: """\
        if len(bars) < {window} + 1:
            return NAN
        _rets = [
            math.log(bars[_i].close / bars[_i - 1].close)
            for _i in range(len(bars) - {window}, len(bars))
        ]
        _n = len(_rets)
        _m = sum(_rets) / _n
        _v = sum((r - _m) ** 2 for r in _rets) / _n
        if _v <= 0:
            return 0.0
        _std = math.sqrt(_v)
        return (sum((r - _m) ** 3 for r in _rets) / _n) / (_std ** 3)
""",
    VolRegimeState: """\
        _short = max(5, {lookback} // 4)
        if len(bars) < {lookback} + 1:
            return NAN
        _long_rets = [
            math.log(bars[_i].close / bars[_i - 1].close)
            for _i in range(len(bars) - {lookback}, len(bars))
        ]
        _short_rets = _long_rets[-_short:]
        _long_var = sum(r * r for r in _long_rets) / len(_long_rets)
        _short_var = sum(r * r for r in _short_rets) / len(_short_rets)
        if _long_var <= 0:
            return 1.0
        _ratio = math.sqrt(_short_var / _long_var)
        if _ratio < 1.0 / {threshold}:
            return 0.0
        if _ratio > {threshold}:
            return 2.0
        return 1.0
""",
    TermStructureSlope: """\
        # Cross-asset feed not yet wired; see compiler.py header note.
        return NAN
""",
    FundingRateDeviation: """\
        # Cross-asset feed not yet wired; see compiler.py header note.
        return NAN
""",
}


# ---------------------------------------------------------------------------
# Compiler implementation.
# ---------------------------------------------------------------------------


class _Compiler:
    """Walks the genome, hoists each unique sub-tree to a helper method, and
    assembles the final module string.
    """

    def __init__(self, genome: Genome) -> None:
        self.genome = genome
        # node_id -> (Pydantic node, method body source)
        self._methods: Dict[str, Tuple[BaseModel, str]] = {}

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    def compile(self) -> str:
        entry_id = self._visit(self.genome.entry)
        exit_id = self._visit(self.genome.exit)
        sizing_src = self._compile_sizing(self.genome.sizing)
        min_history = max(
            _lookback(self.genome.entry),
            _lookback(self.genome.exit),
            self._sizing_lookback(self.genome.sizing),
            2,
        )
        return self._emit_module(entry_id, exit_id, sizing_src, min_history)

    # ------------------------------------------------------------------
    # Tree walk — builds up self._methods.  Returns the node_id of ``node``.
    # ------------------------------------------------------------------

    def _visit(self, node: BaseModel) -> str:
        node_id = _node_id(node)
        if node_id in self._methods:
            return node_id

        if isinstance(node, tuple(_NUMERIC_TEMPLATES.keys())):
            body = self._format_primitive(node)
        elif isinstance(node, WeightedSum):
            child_ids = [self._visit(c) for c in node.children]
            body = self._weighted_sum_body(child_ids, node.weights)
        elif isinstance(node, IfRegime):
            gate_id = self._visit(node.gate)
            true_id = self._visit(node.if_true)
            false_id = self._visit(node.if_false)
            body = self._if_regime_body(gate_id, true_id, false_id)
        elif isinstance(node, (CompareGT, CompareLT)):
            left_id = self._visit(node.left)
            right_id = self._visit(node.right)
            op = ">" if isinstance(node, CompareGT) else "<"
            body = self._compare_body(left_id, right_id, op)
        elif isinstance(node, (CrossOver, CrossUnder)):
            fast_id = self._visit(node.fast)
            slow_id = self._visit(node.slow)
            up = isinstance(node, CrossOver)
            body = self._cross_body(fast_id, slow_id, up)
        elif isinstance(node, ATRBreakout):
            body = self._atr_breakout_body(node.k, node.atr_period, node.atr_mult)
        elif isinstance(node, BoolAnd):
            child_ids = [self._visit(c) for c in node.children]
            body = self._and_body(child_ids)
        elif isinstance(node, BoolOr):
            child_ids = [self._visit(c) for c in node.children]
            body = self._or_body(child_ids)
        elif isinstance(node, BoolNot):
            child_id = self._visit(node.child)
            body = self._not_body(child_id)
        else:
            raise TypeError(f"Unknown node type: {type(node).__name__}")

        self._methods[node_id] = (node, body)
        return node_id

    # ------------------------------------------------------------------
    # Body builders.
    # ------------------------------------------------------------------

    def _format_primitive(self, node: BaseModel) -> str:
        template = _NUMERIC_TEMPLATES[type(node)]
        # Field names appearing in the template are read off the Pydantic dump.
        params: Dict[str, Any] = {}
        for fname in node.__class__.model_fields:
            if fname == "type":
                continue
            params[fname] = getattr(node, fname)
        return template.format(**params)

    @staticmethod
    def _weighted_sum_body(child_ids: List[str], weights: List[float]) -> str:
        terms = [f"        _v{i} = self._n_{cid}(bars)" for i, cid in enumerate(child_ids)]
        nan_check = (
            "        if any(math.isnan(_x) for _x in ("
            + ", ".join(f"_v{i}" for i in range(len(child_ids)))
            + ")):\n            return NAN\n"
        )
        sum_expr = " + ".join(f"({w!r}) * _v{i}" for i, w in enumerate(weights))
        return "\n".join(terms) + "\n" + nan_check + f"        return {sum_expr}\n"

    @staticmethod
    def _if_regime_body(gate_id: str, true_id: str, false_id: str) -> str:
        return (
            f"        if bool(self._n_{gate_id}(bars)):\n"
            f"            return self._n_{true_id}(bars)\n"
            f"        return self._n_{false_id}(bars)\n"
        )

    @staticmethod
    def _compare_body(left_id: str, right_id: str, op: str) -> str:
        return (
            f"        _l = self._n_{left_id}(bars)\n"
            f"        _r = self._n_{right_id}(bars)\n"
            f"        if math.isnan(_l) or math.isnan(_r):\n"
            f"            return False\n"
            f"        return _l {op} _r\n"
        )

    @staticmethod
    def _cross_body(fast_id: str, slow_id: str, up: bool) -> str:
        cmp_now = ">" if up else "<"
        cmp_prev = "<=" if up else ">="
        return (
            "        if len(bars) < 2:\n"
            "            return False\n"
            f"        _fn = self._n_{fast_id}(bars)\n"
            f"        _sn = self._n_{slow_id}(bars)\n"
            f"        _fp = self._n_{fast_id}(bars[:-1])\n"
            f"        _sp = self._n_{slow_id}(bars[:-1])\n"
            "        if any(math.isnan(_x) for _x in (_fn, _sn, _fp, _sp)):\n"
            "            return False\n"
            f"        return _fp {cmp_prev} _sp and _fn {cmp_now} _sn\n"
        )

    @staticmethod
    def _atr_breakout_body(k: int, atr_period: int, atr_mult: float) -> str:
        return textwrap.dedent(f"""\
            if len(bars) < max({k} + 1, {atr_period} + 1):
                return False
            _trs = []
            for _i in range(len(bars) - {atr_period}, len(bars)):
                _h = bars[_i].high
                _l = bars[_i].low
                _pc = bars[_i - 1].close
                _trs.append(max(_h - _l, abs(_h - _pc), abs(_l - _pc)))
            _atr = sum(_trs) / {atr_period}
            _hw = bars[-{k} - 1:-1]
            _rh = max(b.high for b in _hw)
            return bars[-1].close > _rh + ({atr_mult!r}) * _atr
        """)

    @staticmethod
    def _and_body(child_ids: List[str]) -> str:
        calls = " and ".join(f"self._n_{cid}(bars)" for cid in child_ids)
        return f"        return {calls}\n"

    @staticmethod
    def _or_body(child_ids: List[str]) -> str:
        calls = " or ".join(f"self._n_{cid}(bars)" for cid in child_ids)
        return f"        return {calls}\n"

    @staticmethod
    def _not_body(child_id: str) -> str:
        return f"        return not self._n_{child_id}(bars)\n"

    # ------------------------------------------------------------------
    # Sizing.
    # ------------------------------------------------------------------

    def _compile_sizing(self, sizing: BaseModel) -> str:
        if isinstance(sizing, FixedQty):
            return f"        return float({sizing.qty!r})\n"
        if isinstance(sizing, PctOfEquity):
            return (
                "        if bar.close <= 0:\n"
                "            return 0.0\n"
                f"        return (ctx.equity * {sizing.pct / 100.0!r}) / bar.close\n"
            )
        if isinstance(sizing, VolTargeted):
            lookback = sizing.lookback
            target = sizing.target_annual_vol
            return (
                textwrap.dedent(f"""\
                if len(bars) < {lookback} + 1:
                    return 0.0
                _rets = [
                    math.log(bars[_i].close / bars[_i - 1].close)
                    for _i in range(len(bars) - {lookback}, len(bars))
                ]
                _m = sum(_rets) / len(_rets)
                _v = sum((r - _m) ** 2 for r in _rets) / len(_rets)
                if _v <= 0 or bar.close <= 0:
                    return 0.0
                _ann_vol = math.sqrt(_v) * math.sqrt(252)
                _scale = ({target!r}) / _ann_vol if _ann_vol > 0 else 0.0
                return (ctx.equity * _scale) / bar.close
            """)
                .replace("\n", "\n        ")
                .rstrip(" ")
                + "\n"
            )
        raise TypeError(f"Unknown sizing node: {type(sizing).__name__}")

    @staticmethod
    def _sizing_lookback(sizing: BaseModel) -> int:
        if isinstance(sizing, VolTargeted):
            return sizing.lookback + 1
        return 1

    # ------------------------------------------------------------------
    # Module assembly.
    # ------------------------------------------------------------------

    def _emit_module(
        self,
        entry_id: str,
        exit_id: str,
        sizing_src: str,
        min_history: int,
    ) -> str:
        helpers: List[str] = []
        # Sort by node_id for deterministic output (no run-to-run diff churn).
        for node_id, (_node, body) in sorted(self._methods.items()):
            helpers.append(_format_method(f"_n_{node_id}", body))

        helpers_src = "\n".join(helpers)
        genome_hash = _node_id(self.genome)
        hypothesis = (self.genome.hypothesis or "").replace('"""', "'''")[:300]

        return textwrap.dedent(f'''\
            """Compiled strategy {genome_hash}.

            {hypothesis}
            """
            from contract import OrderSide, OrderType, Strategy
            import math

            NAN = float("nan")


            class GeneratedStrategy(Strategy):
                """Auto-generated from genome {genome_hash}."""

                MIN_HISTORY = {min_history}

                def on_bar(self, ctx, bar):
                    bars = ctx.history(bar.symbol, self.MIN_HISTORY + 4)
                    if len(bars) < self.MIN_HISTORY:
                        return

                    _entry = self._n_{entry_id}(bars)
                    _exit = self._n_{exit_id}(bars)

                    pos = ctx.position(bar.symbol)
                    if pos is None and bool(_entry):
                        _qty = self._compute_qty(ctx, bar, bars)
                        if _qty > 0:
                            ctx.submit_order(
                                symbol=bar.symbol,
                                side=OrderSide.LONG,
                                qty=_qty,
                                order_type=OrderType.MARKET,
                                reason="genome:entry",
                            )
                    elif pos is not None and bool(_exit):
                        ctx.submit_order(
                            symbol=bar.symbol,
                            side=OrderSide.SHORT,
                            qty=pos.qty,
                            order_type=OrderType.MARKET,
                            reason="genome:exit",
                        )

                def _compute_qty(self, ctx, bar, bars):
            {textwrap.indent(sizing_src.rstrip(), "        ")}

            {textwrap.indent(helpers_src, "    ")}
            ''')


def _format_method(name: str, body: str) -> str:
    """Wrap a body block (already 8-space indented) in a method definition."""
    body = body.rstrip("\n")
    return f"def {name}(self, bars):\n{body}\n"


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def compile_genome(genome: Genome) -> str:
    """Return a sandbox-ready Python module string for ``genome``.

    The output is deterministic: identical genomes produce byte-identical
    module strings.  Distinct genomes that share sub-trees (e.g. the same
    ``SMA(20)`` referenced in entry and exit) compile to a single helper.
    """
    return _Compiler(genome).compile()
