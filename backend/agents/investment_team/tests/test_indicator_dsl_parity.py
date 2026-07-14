"""Regression tests for the indicator-registry unification refactor.

Targets the specific risks the refactor introduced:

* Cross-DSL numeric parity — factors and synthesis compile indicator math
  from the same canonical source (``indicators.template_bodies`` for
  MACD/ADX/VWAP; ``indicators.registry_metadata`` for lookback formulas).
  Nothing previously asserted the two compilers agree on a shared
  indicator's value for the same params over the same bars; this is the
  test that would have caught the pre-fix VWAP semantics divergence.
* MACD extraction determinism — the shared ``render_macd_body`` streaming
  cache must classify expand/slide/cold-rebuild identically to the
  pre-refactor per-compiler implementations, and repeat compiles of the
  same input must stay byte-identical (genome/spec hashing relies on this).
* ADX loop-slicing perf — the shared ``render_adx_body`` must not scan the
  full retrieved bar window before slicing to the trailing window it needs.
* VWAP rolling-window unification — period-vs-period cache isolation on the
  registry, and rolling behavior parity between the two compilers.
* Executor param-table parity — ``strategy_indicators._INDICATOR_PARAM_VALIDATORS``
  (a literal copy forced by the sandbox's flat-layout constraint) must stay
  in sync with ``indicators.registry_metadata.INDICATOR_METADATA``.
"""

from __future__ import annotations

import math
import sys
import types
from dataclasses import dataclass

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.executor.indicators import INDICATORS
from investment_team.strategy_lab.executor.strategy_indicators import (
    _INDICATOR_PARAM_VALIDATORS,
)
from investment_team.strategy_lab.factors.compiler import _node_id, compile_genome
from investment_team.strategy_lab.factors.models import (
    ADX as FADX,
)
from investment_team.strategy_lab.factors.models import (
    ATR as FATR,
)
from investment_team.strategy_lab.factors.models import (
    EMA as FEMA,
)
from investment_team.strategy_lab.factors.models import (
    RSI as FRSI,
)
from investment_team.strategy_lab.factors.models import (
    SMA as FSMA,
)
from investment_team.strategy_lab.factors.models import (
    VWAP as FVWAP,
)
from investment_team.strategy_lab.factors.models import (
    CompareGT,
    Const,
    FixedQty,
    Genome,
    MACDSignal,
)
from investment_team.strategy_lab.indicators.registry_metadata import INDICATOR_METADATA
from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate
from investment_team.strategy_lab.synthesis import compile_strategy


@dataclass
class _Bar:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = "AAPL"


def _make_bars(n: int, seed: int = 0) -> list[_Bar]:
    """Deterministic synthetic OHLCV series (no RNG — reproducible across runs)."""
    bars = []
    price = 100.0 + seed
    for i in range(n):
        o = price
        h = o + 1.5 + 0.1 * (i % 7)
        low = o - 1.5 - 0.1 * (i % 5)
        c = low + (h - low) * (0.3 + 0.05 * (i % 11))
        v = 1000.0 + 50.0 * (i % 13)
        bars.append(_Bar(timestamp=i, open=o, high=h, low=low, close=c, volume=v))
        price = c
    return bars


@pytest.fixture(autouse=True)
def _stub_contract_module(monkeypatch):
    """Compiled strategy modules import ``from contract import ...`` at module scope."""
    fake = types.ModuleType("contract")

    class _Strategy:
        pass

    class _OrderSide:
        LONG = "LONG"
        SHORT = "SHORT"

    class _OrderType:
        MARKET = "MARKET"

    fake.Strategy = _Strategy
    fake.OrderSide = _OrderSide
    fake.OrderType = _OrderType
    monkeypatch.setitem(sys.modules, "contract", fake)
    yield


def _exec_module(src: str):
    ns: dict = {}
    exec(compile(src, "<compiled>", "exec"), ns)
    return ns


def _spec(entry: EntryRule) -> StrategySpec:
    return StrategySpec(
        strategy_id="parity-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="parity test",
        signal_definition="parity test",
        timeframe="1d",
        entry_rules=[entry],
        exit_rules=[],
        sizing=__import__(
            "investment_team.strategy_lab.spec_dsl", fromlist=["FixedFractionSizing"]
        ).FixedFractionSizing(fraction=0.02),
        target_symbols=["AAPL"],
    )


# ---------------------------------------------------------------------------
# Cross-DSL parity: same indicator+params, same bars -> same value.
# ---------------------------------------------------------------------------

# (label, factors_node, synth_name, synth_params, synth_source, n_values_to_check)
_PARITY_CASES = [
    ("sma", FSMA(period=10), "sma", {"period": 10}, "close"),
    ("ema", FEMA(period=10), "ema", {"period": 10}, "close"),
    ("rsi", FRSI(period=14), "rsi", {"period": 14}, "close"),
    ("atr", FATR(period=14), "atr", {"period": 14}, "close"),
    ("adx", FADX(period=14), "adx", {"period": 14}, "close"),
    (
        "macd_signal",
        MACDSignal(fast=12, slow=26, signal=9),
        "macd",
        {"fast": 12, "slow": 26, "signal": 9, "output": "signal"},
        "close",
    ),
    ("vwap", FVWAP(period=20), "vwap", {"period": 20}, "close"),
]


def _compile_factors_helper(node):
    genome = Genome(
        asset_class="stocks",
        hypothesis="parity",
        entry=CompareGT(left=node, right=Const(value=-1e9)),
        exit=CompareGT(left=Const(value=0.0), right=Const(value=1.0)),
        sizing=FixedQty(qty=1.0),
    )
    src = compile_genome(genome)
    ns = _exec_module(src)
    strat = ns["GeneratedStrategy"]()
    helper_name = f"_n_{_node_id(node)}"
    return getattr(strat, helper_name)


def _compile_synthesis_helper(name: str, params: dict, source: str):
    ref = IndicatorRef(name=name, params=dict(params), source=source)
    entry = EntryRule(side="long", when=Predicate(lhs=ref, op=">", rhs=-1e9))
    src = compile_strategy(_spec(entry))
    ns = _exec_module(src)
    strat = ns["CompiledStrategy"]()
    descriptor = INDICATOR_METADATA[name]
    method = getattr(strat, descriptor.helper_name)
    # Translate DSL param names to the compiled method's kwarg names via the
    # same emit-args table the compiler itself uses (e.g. macd's DSL
    # "output" -> the method's "select" kwarg).
    call_kwargs = {}
    for emit_kwarg, kind, dsl_param in descriptor.emit_args:
        if kind == "source":
            call_kwargs[emit_kwarg] = ref.source
        else:
            call_kwargs[emit_kwarg] = ref.params[dsl_param]
    return lambda bars: method(bars, **call_kwargs)


@pytest.mark.parametrize("label,node,name,params,source", _PARITY_CASES)
def test_factors_and_synthesis_agree_on_shared_indicators(label, node, name, params, source):
    """For every indicator both DSLs share, the compiled factors node and the
    compiled synthesis helper must agree bar-for-bar — at the warm-up
    boundary, mid-stream, and full length — on both warm-up state (NaN/None)
    and numeric value. This is the parity test that would have caught the
    VWAP rolling-vs-cumulative divergence this PR fixes."""
    factors_fn = _compile_factors_helper(node)
    synth_fn = _compile_synthesis_helper(name, params, source)

    bars = _make_bars(80)
    # Check at the warm-up boundary, mid-stream, and full length.
    for n in (35, 55, 80):
        sub = bars[:n]
        f_val = factors_fn(sub)
        s_val = synth_fn(sub)
        f_missing = f_val is None or (isinstance(f_val, float) and math.isnan(f_val))
        s_missing = s_val is None or (isinstance(s_val, float) and math.isnan(s_val))
        assert f_missing == s_missing, (label, n, f_val, s_val)
        if not f_missing:
            assert f_val == pytest.approx(s_val, rel=1e-9, abs=1e-9), (label, n, f_val, s_val)


# ---------------------------------------------------------------------------
# MACD extraction: determinism + streaming-cache classification parity.
# ---------------------------------------------------------------------------


def test_compile_genome_is_byte_deterministic_for_macd():
    """Two compiles of the identical genome must produce byte-identical
    module source — genome hashing and any equality-by-source-text
    consumer elsewhere in the pipeline depend on this."""
    node = MACDSignal(fast=12, slow=26, signal=9)
    genome = Genome(
        asset_class="stocks",
        hypothesis="determinism",
        entry=CompareGT(left=node, right=Const(value=0.0)),
        exit=CompareGT(left=Const(value=0.0), right=Const(value=1.0)),
        sizing=FixedQty(qty=1.0),
    )
    assert compile_genome(genome) == compile_genome(genome)


def test_compile_strategy_is_byte_deterministic_for_macd():
    """Synthesis's counterpart to the genome determinism check above — two
    compiles of the identical spec must produce byte-identical module
    source."""
    ref = IndicatorRef(name="macd", params={"fast": 12, "slow": 26, "signal": 9})
    entry = EntryRule(side="long", when=Predicate(lhs=ref, op=">", rhs=0.0))
    spec = _spec(entry)
    assert compile_strategy(spec) == compile_strategy(spec)


def test_macd_streaming_cache_matches_cold_compute_bar_by_bar():
    """The shared render_macd_body's expand/slide caching must reproduce a
    fresh cold-compute at every step — the exact property the original
    ~200-line duplicated block existed to provide, now from one source."""
    node = MACDSignal(fast=12, slow=26, signal=9)
    genome = Genome(
        asset_class="stocks",
        hypothesis="cache parity",
        entry=CompareGT(left=node, right=Const(value=-1e9)),
        exit=CompareGT(left=Const(value=0.0), right=Const(value=1.0)),
        sizing=FixedQty(qty=1.0),
    )
    strat = _exec_module(compile_genome(genome))["GeneratedStrategy"]()
    helper = getattr(strat, f"_n_{_node_id(node)}")

    reg = IndicatorRegistry()
    bars = _make_bars(120)
    for n in range(1, len(bars) + 1):
        sub = bars[:n]
        got = helper(sub)  # same `strat` instance -> exercises expand caching
        want = reg.macd(sub, fast=12, slow=26, signal=9, source="close", select="signal")
        got_nan = got is None or math.isnan(got)
        want_nan = want is None or math.isnan(want)
        assert got_nan == want_nan, n
        if not got_nan:
            assert got == pytest.approx(want, rel=1e-9, abs=1e-9), n


# ---------------------------------------------------------------------------
# ADX loop-slicing: bounded work independent of total retrieved history.
# ---------------------------------------------------------------------------


class _CountingBars:
    """Wraps a bar list, counting ``__getitem__`` calls made against it directly.

    Nested indexing into a slice this produces (a plain list) is NOT counted —
    the point is to prove the compiled ADX helper touches the *outer* sequence
    only a bounded number of times (the warm-up ``len()`` check plus one
    trailing-window slice), not once per bar in the full history.
    """

    def __init__(self, data):
        self._data = data
        self.getitem_calls = 0

    def __len__(self):
        return len(self._data)

    def __getitem__(self, item):
        self.getitem_calls += 1
        return self._data[item]


def test_adx_scans_bounded_bars_independent_of_history_length():
    node = FADX(period=14)
    genome = Genome(
        asset_class="stocks",
        hypothesis="adx perf",
        entry=CompareGT(left=node, right=Const(value=-1e9)),
        exit=CompareGT(left=Const(value=0.0), right=Const(value=1.0)),
        sizing=FixedQty(qty=1.0),
    )
    strat = _exec_module(compile_genome(genome))["GeneratedStrategy"]()
    helper = getattr(strat, f"_n_{_node_id(node)}")

    bars = _make_bars(500)
    counting = _CountingBars(bars)
    value = helper(counting)
    assert value is not None and not math.isnan(value)
    # Exactly one access to the outer sequence: the bounded trailing-window
    # slice (`bars[-(2*period+1):]`). Independent of len(bars) == 500.
    assert counting.getitem_calls <= 2, counting.getitem_calls

    # A much longer history must not increase the access count.
    long_bars = _make_bars(2000)
    counting_long = _CountingBars(long_bars)
    helper(counting_long)
    assert counting_long.getitem_calls == counting.getitem_calls


def test_synthesis_adx_scans_bounded_bars_independent_of_history_length():
    ref = IndicatorRef(name="adx", params={"period": 14})
    entry = EntryRule(side="long", when=Predicate(lhs=ref, op=">", rhs=-1e9))
    strat = _exec_module(compile_strategy(_spec(entry)))["CompiledStrategy"]()

    bars = _make_bars(500)
    counting = _CountingBars(bars)
    value = strat.adx(counting, period=14)
    assert value is not None and not math.isnan(value)
    assert counting.getitem_calls <= 2, counting.getitem_calls


# ---------------------------------------------------------------------------
# VWAP rolling-window unification.
# ---------------------------------------------------------------------------


def test_registry_vwap_period_none_preserves_cumulative_default():
    reg = IndicatorRegistry()
    bars = _make_bars(60)
    cumulative = reg.vwap(bars)
    rolling_full_window = reg.vwap(bars, period=len(bars))
    assert cumulative == pytest.approx(rolling_full_window, rel=1e-9)


def test_registry_vwap_rolling_window_differs_from_cumulative():
    reg = IndicatorRegistry()
    bars = _make_bars(100)
    cumulative = reg.vwap(bars)
    rolling = reg.vwap(bars, period=20)
    assert cumulative != pytest.approx(rolling, rel=1e-9)
    # Rolling value must equal a fresh registry computed directly over the
    # trailing 20 bars (period-bounded, not an artifact of re-basing).
    fresh = IndicatorRegistry().vwap(bars[-20:])
    assert rolling == pytest.approx(fresh, rel=1e-9)


def test_registry_vwap_distinct_periods_do_not_share_a_cache_slot():
    reg = IndicatorRegistry()
    bars = _make_bars(100)
    v10 = reg.vwap(bars, period=10)
    v50 = reg.vwap(bars, period=50)
    # Re-read each — a collision would have the second call's cache write
    # clobber the first's.
    v10_again = reg.vwap(bars, period=10)
    v50_again = reg.vwap(bars, period=50)
    assert v10 == pytest.approx(v10_again, rel=1e-9)
    assert v50 == pytest.approx(v50_again, rel=1e-9)
    assert v10 != pytest.approx(v50, rel=1e-9)


# ---------------------------------------------------------------------------
# Executor param-table parity: registry_metadata vs. the sandbox's forced
# literal copy in strategy_indicators.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(INDICATOR_METADATA))
def test_param_validator_keys_match_registry_metadata(name):
    """Every param INDICATOR_METADATA declares for `name` must have a matching
    validator in strategy_indicators._INDICATOR_PARAM_VALIDATORS, and vice
    versa — the literal copy the sandbox forces must not drift."""
    descriptor = INDICATOR_METADATA[name]
    expected_keys = set(descriptor.required) | set(descriptor.optional)
    actual_keys = set(_INDICATOR_PARAM_VALIDATORS[name])
    assert actual_keys == expected_keys, (name, actual_keys, expected_keys)


@pytest.mark.parametrize("name", sorted(INDICATOR_METADATA))
def test_param_validator_bounds_match_registry_metadata(name):
    """Spot-check that both validator sets accept/reject the same boundary
    values for every optional int/float param — not just that the key sets
    match, but that the bounds encoded twice (registry_metadata's tuple,
    strategy_indicators' literal copy) still agree."""
    descriptor = INDICATOR_METADATA[name]
    sandbox_validators = _INDICATOR_PARAM_VALIDATORS[name]
    for key, (default, registry_check) in descriptor.optional.items():
        sandbox_check = sandbox_validators[key]
        # The default itself must be accepted by both.
        registry_check(default)
        sandbox_check(default)
        if isinstance(default, bool):
            continue
        if isinstance(default, int):
            probes = (default - 1, default + 1, default * 1000, -(default * 1000) - 1)
        elif isinstance(default, float):
            # Float-appropriate boundary probes: below/above the default,
            # its negation, zero, and a value far outside any sane bound.
            probes = (
                default * 0.5,
                default * 2.0,
                -default,
                0.0,
                default * 1000.0,
                -(default * 1000.0) - 1.0,
            )
        else:
            continue
        for probe in probes:
            registry_ok = _accepts(registry_check, probe)
            sandbox_ok = _accepts(sandbox_check, probe)
            assert registry_ok == sandbox_ok, (name, key, probe, registry_ok, sandbox_ok)


def _accepts(check, value) -> bool:
    try:
        check(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Coverage-probe param-table parity: executor/indicators.py::INDICATORS
# (consulted only by the static coverage probe's AST dispatcher) must stay
# in sync with INDICATOR_METADATA's numeric/float params. Selector params
# ("output"/"band" — the "select" kind in emit_args) are deliberately
# excluded: INDICATORS' helpers return the whole tuple (macd/bollinger_bands/
# stochastic/donchian_channels/keltner_channels all have tuple_arity set),
# so they have no selector kwarg to match; "source" is likewise excluded —
# the probe threads it via `data_inputs`, not a kwarg_names entry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(INDICATOR_METADATA))
def test_coverage_probe_registry_kwarg_names_match_registry_metadata(name):
    descriptor = INDICATOR_METADATA[name]
    spec = INDICATORS[descriptor.helper_name]
    expected = {
        dsl_param
        for _emit_kwarg, kind, dsl_param in descriptor.emit_args
        if kind in ("int", "float")
    }
    assert set(spec.kwarg_names) == expected, (
        name,
        descriptor.helper_name,
        set(spec.kwarg_names),
        expected,
    )


# ---------------------------------------------------------------------------
# VWAP unification completeness: the sandbox-exposed scalar `vwap()` must
# compute the SAME rolling-window value as the engine/DSL/`ctx.indicator`
# path and the coverage-probe reference — not the old cumulative value.
# The scalar is what free-form strategies call (`from indicators import vwap`)
# and what the coverage probe models via `_windowed_vwap`; a cumulative scalar
# there diverges from the rolling engine that re-evaluates it (conformance
# gate) and from the rolling probe that scores it. This test would have caught
# that divergence.
# ---------------------------------------------------------------------------


def test_scalar_vwap_matches_engine_rolling_not_cumulative():
    from investment_team.strategy_lab.executor import strategy_indicators as si

    bars = _make_bars(80)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]

    scalar = si.vwap(highs, lows, closes, vols)  # default period=20
    engine_rolling = IndicatorRegistry().vwap(bars, period=20)
    engine_cumulative = IndicatorRegistry().vwap(bars)  # period=None -> cumulative

    assert scalar == pytest.approx(engine_rolling, rel=1e-9)
    # The unification actually changed the value — guard against a silent
    # revert to cumulative that would still pass the equality above if
    # rolling and cumulative happened to coincide on some fixture.
    assert engine_rolling != pytest.approx(engine_cumulative, rel=1e-9)
    # The scalar also honours an explicit period, matching what INDICATORS
    # advertises to the probe (`kwarg_names=('period',)`).
    assert si.vwap(highs, lows, closes, vols, period=30) == pytest.approx(
        IndicatorRegistry().vwap(bars, period=30), rel=1e-9
    )


def test_render_functions_reject_unresolved_markers():
    from investment_team.strategy_lab.indicators import template_bodies as tb

    with pytest.raises(ValueError, match="unresolved template marker"):
        tb._assert_resolved("if len(%BARS%) < 1:\n    return %MISSING%")
    # A fully-substituted body with `{period}` still present (the caller's to
    # fill) is accepted unchanged.
    assert tb._assert_resolved("_w = bars[-{period}:]") == "_w = bars[-{period}:]"
