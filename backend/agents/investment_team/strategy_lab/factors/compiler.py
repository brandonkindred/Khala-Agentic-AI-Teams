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

Indentation rule
----------------

To keep emit logic auditable, every body builder in this module returns
**left-aligned** text (no leading whitespace; internal Python
indentation only).  ``_format_method`` adds the 8-space method-body
indent, and the final assembly in ``_emit_module`` writes module-level
text at column 0 directly.  No ``textwrap.dedent`` gymnastics on the
outer template — the previous version did and produced
``IndentationError`` on every compiled strategy
(see PR #356 codex review thread).
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel

from ..indicators.registry_metadata import lookback_for
from ..indicators.template_bodies import render_adx_body, render_macd_body, render_vwap_body
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
        return lookback_for("rsi", {"period": node.period})
    if isinstance(node, MACDSignal):
        # Routed through the shared ``registry_metadata.lookback_for`` so this
        # formula (``slow + signal - 1``) has exactly one source, consulted by
        # both DSL compilers and the registry's own signal-line gate.
        #
        # BACKWARDS-COMPATIBILITY NOTE: prior revisions returned
        # ``slow + signal`` (one bar later). Genomes with MACDSignal
        # compiled against the old code fire signals one bar later than
        # they do now. Any persisted backtest output (equity curves,
        # trade records, deflated Sharpe values) pre-dating this fix
        # will not bit-reproduce against re-runs through the corrected
        # compiler. The change is INTENTIONAL — it brings factor-DSL
        # into alignment with the registry, synthesis compiler, and the
        # reference primitive — but downstream pipelines that hash or
        # diff stored backtest artefacts may need to re-baseline.
        return lookback_for(
            "macd", {"slow": node.slow, "signal": node.signal, "output": "signal"}
        )
    if isinstance(node, ATR):
        return lookback_for("atr", {"period": node.period})
    if isinstance(node, ADX):
        return lookback_for("adx", {"period": node.period})
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
# Per-node-type method-body templates.  Every template is LEFT-ALIGNED — no
# leading whitespace on any line.  ``_format_method`` adds the method-body
# indent uniformly via ``textwrap.indent`` so we never have to reason about
# how interpolation interacts with surrounding indentation.
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
    MACDSignal: render_macd_body(
        bars_var="bars",
        missing="NAN",
        # Cache key embeds ``source='close'`` (the only source the factor
        # DSL's MACDSignal supports today) so future cross-source extensions
        # cannot silently collide. Matches the registry and synthesis key
        # shapes.
        cache_key_expr="('macd_signal_{fast}_{slow}_{signal}_close', _symbol)",
        # MACDSignal is always the signal-line output — the shared body
        # computes macd/signal/histogram together and this pins the select.
        select_expr="'signal'",
        close_expr_template="{obj}.close",
    ),
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
    ADX: render_adx_body(bars_var="bars", missing="NAN"),
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
    VWAP: render_vwap_body(bars_var="bars", missing="NAN"),
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
        # node_id -> (Pydantic node, left-aligned method body source)
        self._methods: Dict[str, Tuple[BaseModel, str]] = {}
        # ``_node_id`` computes a sha256 over the canonical JSON of each node;
        # memoise it on object identity for the compile's lifetime so a node
        # the tree-walk revisits isn't re-serialised + re-hashed. Every node
        # whose id we key on stays referenced for the whole compile — genome
        # nodes via ``self.genome``, and any node synthesised during the walk
        # via ``self._synthesized`` — so ``id()`` cannot be recycled into a
        # false hit.
        self._node_id_memo: Dict[int, str] = {}
        self._synthesized: List[BaseModel] = []

    def _nid(self, node: BaseModel) -> str:
        """Memoised ``_node_id`` keyed on object identity.

        Postconditions: returns ``_node_id(node)``; repeated calls for the
        same live object return the cached value without re-hashing.
        """
        key = id(node)
        cached = self._node_id_memo.get(key)
        if cached is not None:
            return cached
        result = _node_id(node)
        self._node_id_memo[key] = result
        return result

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
        node_id = self._nid(node)
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
            # Common-sub-expression elimination: route the ATR computation
            # through a shared ATR helper instead of inlining the true-range
            # loop. When the genome also references an ``ATR(atr_period)``
            # primitive elsewhere, ``_visit`` dedupes to a single emitted
            # helper, so the ATR series is computed once rather than twice.
            atr_node = ATR(period=node.atr_period)
            self._synthesized.append(atr_node)
            atr_id = self._visit(atr_node)
            body = self._atr_breakout_body(node.k, node.atr_period, node.atr_mult, atr_id)
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
    # Body builders — every builder returns a left-aligned string with
    # internal Python indentation only.  No leading whitespace on any line.
    # ------------------------------------------------------------------

    def _format_primitive(self, node: BaseModel) -> str:
        template = _NUMERIC_TEMPLATES[type(node)]
        params: Dict[str, Any] = {}
        for fname in node.__class__.model_fields:
            if fname == "type":
                continue
            params[fname] = getattr(node, fname)
        # Compile-time precondition for MACDSignal: the template interpolates
        # ``{fast}``, ``{slow}``, ``{signal}`` directly into the emitted source
        # (``not ({signal} >= 2)``). If a validation-bypass path lets a
        # ``float('nan')`` slip into the genome, ``repr(float('nan'))`` is
        # ``'nan'`` — emitted code would reference unbound identifier ``nan``
        # and the module would raise ``NameError`` at exec time instead of
        # the documented runtime gate. Reject malformed params loudly here
        # before any source string is built. Spec_dsl normally validates
        # these as ``int`` ge 2 so this is defence-in-depth.
        if isinstance(node, MACDSignal):
            for pname in ("fast", "slow", "signal"):
                val = params[pname]
                if not isinstance(val, int) or isinstance(val, bool):
                    raise TypeError(
                        f"MACDSignal.{pname} must be an int, got {type(val).__name__}={val!r}"
                    )
                if val < 2:
                    raise ValueError(f"MACDSignal.{pname} must be >= 2 at compile time, got {val}")
        return template.format(**params)

    @staticmethod
    def _weighted_sum_body(child_ids: List[str], weights: List[float]) -> str:
        terms = [f"_v{i} = self._n_{cid}(bars)" for i, cid in enumerate(child_ids)]
        nan_args = ", ".join(f"_v{i}" for i in range(len(child_ids)))
        sum_expr = " + ".join(f"({w!r}) * _v{i}" for i, w in enumerate(weights))
        return (
            "\n".join(terms)
            + "\n"
            + f"if any(math.isnan(_x) for _x in ({nan_args})):\n"
            + "    return NAN\n"
            + f"return {sum_expr}\n"
        )

    @staticmethod
    def _if_regime_body(gate_id: str, true_id: str, false_id: str) -> str:
        return (
            f"if bool(self._n_{gate_id}(bars)):\n"
            f"    return self._n_{true_id}(bars)\n"
            f"return self._n_{false_id}(bars)\n"
        )

    @staticmethod
    def _compare_body(left_id: str, right_id: str, op: str) -> str:
        return (
            f"_l = self._n_{left_id}(bars)\n"
            f"_r = self._n_{right_id}(bars)\n"
            f"if math.isnan(_l) or math.isnan(_r):\n"
            f"    return False\n"
            f"return _l {op} _r\n"
        )

    @staticmethod
    def _cross_body(fast_id: str, slow_id: str, up: bool) -> str:
        cmp_now = ">" if up else "<"
        cmp_prev = "<=" if up else ">="
        return (
            "if len(bars) < 2:\n"
            "    return False\n"
            f"_fn = self._n_{fast_id}(bars)\n"
            f"_sn = self._n_{slow_id}(bars)\n"
            f"_fp = self._n_{fast_id}(bars[:-1])\n"
            f"_sp = self._n_{slow_id}(bars[:-1])\n"
            "if any(math.isnan(_x) for _x in (_fn, _sn, _fp, _sp)):\n"
            "    return False\n"
            f"return _fp {cmp_prev} _sp and _fn {cmp_now} _sn\n"
        )

    @staticmethod
    def _atr_breakout_body(k: int, atr_period: int, atr_mult: float, atr_id: str) -> str:
        # ``_atr`` is read from the shared ATR helper (``_n_<atr_id>``) rather
        # than recomputed inline. The warmup guard still gates on
        # ``atr_period + 1`` so the helper never returns NAN here — matching
        # the previous inline ``sum(_trs) / atr_period`` value exactly.
        return (
            f"if len(bars) < max({k} + 1, {atr_period} + 1):\n"
            "    return False\n"
            f"_atr = self._n_{atr_id}(bars)\n"
            f"_hw = bars[-{k} - 1:-1]\n"
            "_rh = max(b.high for b in _hw)\n"
            f"return bars[-1].close > _rh + ({atr_mult!r}) * _atr\n"
        )

    @staticmethod
    def _and_body(child_ids: List[str]) -> str:
        calls = " and ".join(f"self._n_{cid}(bars)" for cid in child_ids)
        return f"return {calls}\n"

    @staticmethod
    def _or_body(child_ids: List[str]) -> str:
        calls = " or ".join(f"self._n_{cid}(bars)" for cid in child_ids)
        return f"return {calls}\n"

    @staticmethod
    def _not_body(child_id: str) -> str:
        return f"return not self._n_{child_id}(bars)\n"

    # ------------------------------------------------------------------
    # Sizing.  Body builders return left-aligned text the same way; the
    # _emit_module assembly indents it once into the _compute_qty method.
    # ------------------------------------------------------------------

    def _compile_sizing(self, sizing: BaseModel) -> str:
        if isinstance(sizing, FixedQty):
            return f"return float({sizing.qty!r})\n"
        if isinstance(sizing, PctOfEquity):
            pct_frac = sizing.pct / 100.0
            return (
                "if bar.close <= 0:\n"
                "    return 0.0\n"
                f"return (ctx.equity * {pct_frac!r}) / bar.close\n"
            )
        if isinstance(sizing, VolTargeted):
            lookback = sizing.lookback
            target = sizing.target_annual_vol
            return (
                f"if len(bars) < {lookback} + 1:\n"
                "    return 0.0\n"
                "_rets = [\n"
                "    math.log(bars[_i].close / bars[_i - 1].close)\n"
                f"    for _i in range(len(bars) - {lookback}, len(bars))\n"
                "]\n"
                "_m = sum(_rets) / len(_rets)\n"
                "_v = sum((r - _m) ** 2 for r in _rets) / len(_rets)\n"
                "if _v <= 0 or bar.close <= 0:\n"
                "    return 0.0\n"
                "_ann_vol = math.sqrt(_v) * math.sqrt(252)\n"
                f"_scale = ({target!r}) / _ann_vol if _ann_vol > 0 else 0.0\n"
                "return (ctx.equity * _scale) / bar.close\n"
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
        # Each helper becomes a complete method block at column 0 (def at 0,
        # body at 4).  We then indent the entire class body by 4 below.
        helper_blocks: List[str] = []
        for node_id, (_node, body) in sorted(self._methods.items()):
            helper_blocks.append(_format_method(f"_n_{node_id}", body))
        helpers_src = "\n\n".join(helper_blocks)

        # Sizing body lives inside _compute_qty (8-space indent in final output;
        # 4-space here at the class-body-pre-indent level).
        sizing_indented = textwrap.indent(sizing_src.rstrip("\n"), "    ")

        genome_hash = self._nid(self.genome)
        hypothesis = (self.genome.hypothesis or "").replace('"""', "'''")

        # Build the class body at column 0, then indent the whole thing 4
        # spaces in one shot.  Module-level statements (docstring, imports,
        # NAN, class def) stay at column 0.
        class_body_lines = [
            f'"""Auto-generated from genome {genome_hash}."""',
            "",
            f"MIN_HISTORY = {min_history}",
            "",
            "def __init__(self):",
            "    super().__init__()",
            "    # Per-instance state for streaming-recurrence indicators (e.g. macd_signal)",
            "    # — empty dict the helper methods populate lazily.",
            "    self._ind_state = {}",
            "",
            "def on_bar(self, ctx, bar):",
            "    bars = ctx.history(bar.symbol, self.MIN_HISTORY + 4)",
            "    if len(bars) < self.MIN_HISTORY:",
            "        return",
            "",
            f"    _entry = self._n_{entry_id}(bars)",
            f"    _exit = self._n_{exit_id}(bars)",
            "",
            "    pos = ctx.position(bar.symbol)",
            "    if pos is None and bool(_entry):",
            "        _qty = self._compute_qty(ctx, bar, bars)",
            "        if _qty > 0:",
            "            ctx.submit_order(",
            "                symbol=bar.symbol,",
            "                side=OrderSide.LONG,",
            "                qty=_qty,",
            "                order_type=OrderType.MARKET,",
            '                reason="genome:entry",',
            "            )",
            "    elif pos is not None and bool(_exit):",
            "        ctx.submit_order(",
            "            symbol=bar.symbol,",
            "            side=OrderSide.SHORT,",
            "            qty=pos.qty,",
            "            order_type=OrderType.MARKET,",
            '            reason="genome:exit",',
            "        )",
            "",
            "def _compute_qty(self, ctx, bar, bars):",
            sizing_indented,
            "",
            helpers_src,
        ]
        class_body = "\n".join(class_body_lines)
        indented_class_body = textwrap.indent(class_body, "    ")

        # ``deque`` is only emitted when the genome includes a MACDSignal
        # node — keeps non-MACD strategy modules clean and avoids
        # spurious F401 warnings under any future linter pass over
        # generated code.
        needs_deque = any(isinstance(node, MACDSignal) for node, _ in self._methods.values())
        deque_import = "from collections import deque\n" if needs_deque else ""

        return (
            f'"""Compiled strategy {genome_hash}.\n'
            "\n"
            f"{hypothesis}\n"
            '"""\n'
            "from contract import OrderSide, OrderType, Strategy\n"
            "import math\n"
            f"{deque_import}"
            "\n"
            'NAN = float("nan")\n'
            "\n"
            "\n"
            "class GeneratedStrategy(Strategy):\n"
            f"{indented_class_body}\n"
        )


def _format_method(name: str, body_la: str) -> str:
    """Wrap a left-aligned ``body_la`` in a ``def {name}(self, bars):`` block.

    The ``def`` line lands at column 0; the body is uniformly indented by
    4 spaces.  When the surrounding ``_emit_module`` indents the class body
    by another 4 spaces, the final layout becomes ``def`` at column 4 and
    body at column 8 — standard Python.
    """
    body_la = body_la.rstrip("\n")
    body_indented = textwrap.indent(body_la, "    ")
    return f"def {name}(self, bars):\n{body_indented}"


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
