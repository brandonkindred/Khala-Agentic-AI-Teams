"""Coverage for the expanded indicator catalogue.

Adds Donchian/Keltner channels, Bollinger %B/bandwidth, OBV, MFI, ROC, CCI and
Williams %R across every layer they touch:

* **Param validation** — the DSL ``IndicatorRef`` registry rejects out-of-range
  / mistyped params and unknown selectors, and honours ``allow_source``.
* **Streaming correctness** — the registry's cold-start, bar-by-bar (warm-path)
  and sliding-window outputs all agree with an independent reference, and the
  bounded oscillators stay inside their declared ``output_range``.
* **Scalar wrappers + accessor** — ``executor.strategy_indicators`` helpers and
  ``indicator_value`` return the registry's trailing value.
* **Compile + conformance** — each indicator compiles into a runnable strategy,
  the inline helper matches the registry, and a custom-code strategy reading the
  indicator passes / fails the predicate-conformance gate as expected.
"""

from __future__ import annotations

import random
import sys
import textwrap
import types
from dataclasses import dataclass
from typing import List, Optional

import pytest
from pydantic import ValidationError

from investment_team.models import StrategySpec
from investment_team.strategy_lab.executor import indicators as pdi
from investment_team.strategy_lab.executor import strategy_indicators as si
from investment_team.strategy_lab.executor.strategy_indicators import indicator_value
from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry
from investment_team.strategy_lab.quality_gates.predicate_conformance import (
    PredicateConformanceGate,
)
from investment_team.strategy_lab.quality_gates.predicate_conformance_fixtures import (
    generate_conformance_fixtures,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    IndicatorRef,
    Predicate,
    StopLossRule,
    format_rules_for_prompt,
)
from investment_team.strategy_lab.synthesis.compiler import compile_strategy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Bar:
    timestamp: str
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 1.0
    symbol: str = "X"


def _series(n: int, seed: int = 0) -> List[_Bar]:
    rng = random.Random(seed)
    bars: List[_Bar] = []
    close = 100.0
    for i in range(n):
        close = max(5.0, close + rng.uniform(-3.0, 3.0) + i * 0.05)
        spread = 1.0 + rng.uniform(0.0, 0.5)
        bars.append(
            _Bar(
                timestamp=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=close - 0.1,
                high=close + spread,
                low=max(0.5, close - spread),
                close=close,
                volume=1000.0 + (i % 7) * 250.0,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Independent reference implementations (mirror the registry math).
# ---------------------------------------------------------------------------


def _wema(vals: List[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    v = vals[0]
    for x in vals[1:]:
        v = alpha * x + (1.0 - alpha) * v
    return v


def _ref_donchian(bars, period: int, select: str) -> Optional[float]:
    if len(bars) < period:
        return None
    w = bars[-period:]
    upper = max(b.high for b in w)
    lower = min(b.low for b in w)
    return {"upper": upper, "lower": lower, "middle": (upper + lower) / 2.0}[select]


def _ref_keltner(bars, period: int, atr_period: int, mult: float, select: str) -> Optional[float]:
    if len(bars) < max(period, atr_period + 1):
        return None
    middle = _wema([b.close for b in bars[-period:]], period)
    total = 0.0
    for i in range(len(bars) - atr_period, len(bars)):
        h, low, pc = bars[i].high, bars[i].low, bars[i - 1].close
        total += max(h - low, abs(h - pc), abs(low - pc))
    atr_val = total / atr_period
    return {
        "middle": middle,
        "upper": middle + mult * atr_val,
        "lower": middle - mult * atr_val,
    }[select]


def _ref_obv(bars) -> Optional[float]:
    if not bars:
        return None
    val = 0.0
    for i in range(1, len(bars)):
        if bars[i].close > bars[i - 1].close:
            val += bars[i].volume
        elif bars[i].close < bars[i - 1].close:
            val -= bars[i].volume
    return val


def _ref_mfi(bars, period: int) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    pos = neg = 0.0
    for i in range(len(bars) - period, len(bars)):
        tp = (bars[i].high + bars[i].low + bars[i].close) / 3.0
        tpp = (bars[i - 1].high + bars[i - 1].low + bars[i - 1].close) / 3.0
        rmf = tp * bars[i].volume
        if tp > tpp:
            pos += rmf
        elif tp < tpp:
            neg += rmf
    if neg == 0:
        return 100.0 if pos > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + pos / neg)


def _ref_roc(bars, period: int) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    cur, prev = bars[-1].close, bars[-1 - period].close
    return 0.0 if prev == 0 else (cur - prev) / prev * 100.0


def _ref_cci(bars, period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    tps = [(b.high + b.low + b.close) / 3.0 for b in bars[-period:]]
    sma_tp = sum(tps) / period
    md = sum(abs(t - sma_tp) for t in tps) / period
    return 0.0 if md == 0 else (tps[-1] - sma_tp) / (0.015 * md)


def _ref_williams(bars, period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    w = bars[-period:]
    hi = max(b.high for b in w)
    lo = min(b.low for b in w)
    rng = hi - lo
    return -50.0 if rng == 0 else -100.0 * (hi - bars[-1].close) / rng


def _ref_bb(bars, period: int, num_std: float, select: str) -> Optional[float]:
    if len(bars) < period:
        return None
    vals = [b.close for b in bars[-period:]]
    mean = sum(vals) / period
    var = max(0.0, sum(v * v for v in vals) / period - mean * mean)
    std = var**0.5
    upper, lower = mean + num_std * std, mean - num_std * std
    if select == "percent_b":
        width = upper - lower
        return 0.5 if width == 0 else (bars[-1].close - lower) / width
    if select == "bandwidth":
        return 0.0 if mean == 0 else (upper - lower) / mean
    raise AssertionError(select)


# (id, registry-call, reference-call, warmup_min) — one row per new output.
_CASES = [
    (
        "donchian_upper",
        lambda r, b: r.donchian(b, 20, "upper"),
        lambda b: _ref_donchian(b, 20, "upper"),
        20,
    ),
    (
        "donchian_middle",
        lambda r, b: r.donchian(b, 20, "middle"),
        lambda b: _ref_donchian(b, 20, "middle"),
        20,
    ),
    (
        "donchian_lower",
        lambda r, b: r.donchian(b, 20, "lower"),
        lambda b: _ref_donchian(b, 20, "lower"),
        20,
    ),
    (
        "keltner_upper",
        lambda r, b: r.keltner(b, 20, 10, 1.5, "upper"),
        lambda b: _ref_keltner(b, 20, 10, 1.5, "upper"),
        20,
    ),
    (
        "keltner_lower",
        lambda r, b: r.keltner(b, 20, 10, 1.5, "lower"),
        lambda b: _ref_keltner(b, 20, 10, 1.5, "lower"),
        20,
    ),
    ("obv", lambda r, b: r.obv(b), lambda b: _ref_obv(b), 2),
    ("mfi", lambda r, b: r.mfi(b, 14), lambda b: _ref_mfi(b, 14), 15),
    ("roc", lambda r, b: r.roc(b, 12), lambda b: _ref_roc(b, 12), 13),
    ("cci", lambda r, b: r.cci(b, 20), lambda b: _ref_cci(b, 20), 20),
    ("williams_r", lambda r, b: r.williams_r(b, 14), lambda b: _ref_williams(b, 14), 14),
    (
        "bb_percent_b",
        lambda r, b: r.bollinger_bands(b, 20, 2.0, select="percent_b"),
        lambda b: _ref_bb(b, 20, 2.0, "percent_b"),
        20,
    ),
    (
        "bb_bandwidth",
        lambda r, b: r.bollinger_bands(b, 20, 2.0, select="bandwidth"),
        lambda b: _ref_bb(b, 20, 2.0, "bandwidth"),
        20,
    ),
]
_CASE_IDS = [c[0] for c in _CASES]


# ---------------------------------------------------------------------------
# Streaming correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_cold_start_matches_reference(case) -> None:
    """A fresh registry at every history depth must match the reference."""
    _id, reg_call, ref_call, warm = case
    bars = _series(80, seed=11)
    for n in range(warm, len(bars) + 1):
        sub = bars[:n]
        got = reg_call(IndicatorRegistry(), sub)
        exp = ref_call(sub)
        assert got == pytest.approx(exp, rel=0, abs=1e-9), f"{_id} n={n}"


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_streaming_bar_by_bar_matches_reference(case) -> None:
    """One registry driven bar-by-bar (warm path) must match the reference."""
    _id, reg_call, ref_call, warm = case
    bars = _series(80, seed=37)
    reg = IndicatorRegistry()
    for n in range(warm, len(bars) + 1):
        sub = bars[:n]
        got = reg_call(reg, sub)
        exp = ref_call(sub)
        assert got == pytest.approx(exp, rel=0, abs=1e-9), f"{_id} n={n}"


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_sliding_window_matches_cold_compute(case) -> None:
    """A registry driven with a fixed-length sliding window matches a cold compute."""
    _id, reg_call, ref_call, _warm = case
    bars = _series(120, seed=88)
    window = 50
    reg = IndicatorRegistry()
    for offset in range(0, len(bars) - window + 1):
        sliding = bars[offset : offset + window]
        got = reg_call(reg, sliding)
        cold = reg_call(IndicatorRegistry(), sliding)
        assert got == pytest.approx(cold, rel=0, abs=1e-9), f"{_id} offset={offset}"


def test_same_bar_repeat_returns_cached() -> None:
    bars = _series(60, seed=5)
    reg = IndicatorRegistry()
    for _id, reg_call, _ref, _warm in _CASES:
        first = reg_call(reg, bars)
        second = reg_call(reg, bars)
        assert first == pytest.approx(second), _id


def test_warmup_returns_none() -> None:
    reg = IndicatorRegistry()
    assert reg.donchian(_series(10), 20, "upper") is None
    assert reg.keltner(_series(10), 20, 10, 2.0, "middle") is None
    assert reg.mfi(_series(5), 14) is None
    assert reg.roc(_series(5), 12) is None
    assert reg.cci(_series(10), 20) is None
    assert reg.williams_r(_series(10), 14) is None
    assert reg.obv([]) is None


def test_bounded_oscillators_stay_in_range() -> None:
    bars = _series(120, seed=21)
    reg = IndicatorRegistry()
    for n in range(16, len(bars) + 1):
        sub = bars[:n]
        m = reg.mfi(sub, 14)
        w = reg.williams_r(sub, 14)
        if m is not None:
            assert 0.0 <= m <= 100.0
        if w is not None:
            assert -100.0 <= w <= 0.0


def test_flat_window_neutral_conventions() -> None:
    """Degenerate (flat) windows return finite neutral values, never divide-by-zero."""
    flat = [
        _Bar(timestamp=f"t{i}", open=50, high=50, low=50, close=50, volume=100) for i in range(30)
    ]
    reg = IndicatorRegistry()
    assert reg.williams_r(flat, 14) == -50.0
    assert reg.cci(flat, 20) == 0.0
    assert reg.mfi(flat, 14) == 50.0  # no up/down typical-price moves
    assert reg.obv(flat) == 0.0
    assert reg.bollinger_bands(flat, 20, 2.0, select="percent_b") == 0.5
    assert reg.bollinger_bands(flat, 20, 2.0, select="bandwidth") == 0.0


# ---------------------------------------------------------------------------
# DSL param validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,params",
    [
        ("donchian", {"period": 1}),
        ("donchian", {"period": 401}),
        ("donchian", {"period": 2.5}),
        ("donchian", {"band": "nope"}),
        ("keltner", {"multiplier": 0}),
        ("keltner", {"atr_period": 1}),
        ("keltner", {"period": 401}),
        ("mfi", {"period": 1}),
        ("mfi", {"period": 201}),
        ("roc", {"period": 1}),
        ("roc", {"period": 401}),
        ("cci", {"period": 1}),
        ("williams_r", {"period": 1}),
        ("williams_r", {"period": 201}),
        ("obv", {"period": 5}),  # OBV takes no params
    ],
)
def test_param_validation_rejects(name, params) -> None:
    with pytest.raises(ValidationError):
        IndicatorRef(name=name, params=params)


@pytest.mark.parametrize("name", ["donchian", "keltner", "obv", "mfi", "cci", "williams_r"])
def test_ohlc_indicators_reject_source_override(name) -> None:
    with pytest.raises(ValidationError):
        IndicatorRef(name=name, source="high")


def test_roc_accepts_source_override() -> None:
    ref = IndicatorRef(name="roc", source="hl2", params={"period": 5})
    assert ref.source == "hl2"
    assert ref.param("period") == 5


@pytest.mark.parametrize("band", ["percent_b", "bandwidth", "upper", "middle", "lower"])
def test_bollinger_new_bands_accepted(band) -> None:
    ref = IndicatorRef(name="bollinger", params={"band": band})
    assert ref.param("band") == band


@pytest.mark.parametrize(
    "ref,fragment",
    [
        (
            IndicatorRef(name="donchian", params={"band": "upper", "period": 20}),
            "donchian_upper(20)",
        ),
        (
            IndicatorRef(
                name="keltner",
                params={"band": "lower", "period": 20, "atr_period": 10, "multiplier": 1.5},
            ),
            "keltner_lower(20,10,1.5)",
        ),
        (IndicatorRef(name="obv"), "obv()"),
        (IndicatorRef(name="mfi", params={"period": 14}), "mfi(14)"),
        (IndicatorRef(name="roc", params={"period": 12}), "roc(12)"),
        (IndicatorRef(name="roc", params={"period": 12}, source="hl2"), "roc(12, source=hl2)"),
        (IndicatorRef(name="cci", params={"period": 20}), "cci(20)"),
        (IndicatorRef(name="williams_r", params={"period": 14}), "williams_r(14)"),
    ],
)
def test_prose_formatter_renders_new_indicators(ref, fragment) -> None:
    rules = [EntryRule(side="long", when=Predicate(lhs=ref, op="<", rhs=0.0))]
    assert fragment in format_rules_for_prompt(rules)


def test_defaults_filled_for_new_indicators() -> None:
    assert IndicatorRef(name="donchian").param("period") == 20
    assert IndicatorRef(name="keltner").param("atr_period") == 10
    assert IndicatorRef(name="keltner").param("multiplier") == 2.0
    assert IndicatorRef(name="mfi").param("period") == 14
    assert IndicatorRef(name="roc").param("period") == 12
    assert IndicatorRef(name="cci").param("period") == 20
    assert IndicatorRef(name="williams_r").param("period") == 14


# ---------------------------------------------------------------------------
# Scalar wrappers + accessor
# ---------------------------------------------------------------------------


def test_scalar_wrappers_match_registry() -> None:
    bars = _series(80, seed=6)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    reg = IndicatorRegistry()

    assert si.obv(closes, vols) == pytest.approx(reg.obv(bars))
    assert si.mfi(highs, lows, closes, vols, 14) == pytest.approx(reg.mfi(bars, 14))
    assert si.roc(closes, 12) == pytest.approx(reg.roc(bars, 12))
    assert si.cci(highs, lows, closes, 20) == pytest.approx(reg.cci(bars, 20))
    assert si.williams_r(highs, lows, closes, 14) == pytest.approx(reg.williams_r(bars, 14))

    d_u, d_m, d_l = si.donchian_channels(highs, lows, 20)
    assert d_u == pytest.approx(reg.donchian(bars, 20, "upper"))
    assert d_m == pytest.approx(reg.donchian(bars, 20, "middle"))
    assert d_l == pytest.approx(reg.donchian(bars, 20, "lower"))

    k_u, k_m, k_l = si.keltner_channels(highs, lows, closes, 20, 10, 1.5)
    assert k_u == pytest.approx(reg.keltner(bars, 20, 10, 1.5, "upper"))
    assert k_l == pytest.approx(reg.keltner(bars, 20, 10, 1.5, "lower"))


def test_accessor_matches_registry() -> None:
    bars = _series(80, seed=8)
    reg = IndicatorRegistry()
    assert indicator_value("williams_r", bars, period=14) == pytest.approx(reg.williams_r(bars, 14))
    assert indicator_value("donchian", bars, band="upper", period=20) == pytest.approx(
        reg.donchian(bars, 20, "upper")
    )
    assert indicator_value(
        "keltner", bars, band="lower", period=20, atr_period=10, multiplier=1.5
    ) == pytest.approx(reg.keltner(bars, 20, 10, 1.5, "lower"))
    assert indicator_value("obv", bars) == pytest.approx(reg.obv(bars))
    assert indicator_value("mfi", bars, period=14) == pytest.approx(reg.mfi(bars, 14))
    assert indicator_value("cci", bars, period=20) == pytest.approx(reg.cci(bars, 20))
    assert indicator_value("roc", bars, period=10, source="close") == pytest.approx(
        reg.roc(bars, 10)
    )
    assert indicator_value("bollinger", bars, band="percent_b", period=20) == pytest.approx(
        reg.bollinger_bands(bars, 20, 2.0, select="percent_b")
    )


def test_accessor_rejects_bad_params() -> None:
    bars = _series(40)
    with pytest.raises(ValueError):
        indicator_value("mfi", bars, period=1)
    with pytest.raises(ValueError):
        indicator_value("obv", bars, source="high")  # OHLC indicator, no source override
    with pytest.raises(ValueError):
        indicator_value("donchian", bars, band="nope")


# ---------------------------------------------------------------------------
# Compile + conformance
# ---------------------------------------------------------------------------


@pytest.fixture
def _contract_module():
    """Install a minimal ``contract`` module so compiled strategies exec."""
    mod = types.ModuleType("contract")

    class Strategy:  # noqa: D401 - test stub
        def __init__(self):
            pass

    mod.Strategy = Strategy
    sys.modules["contract"] = mod
    yield
    sys.modules.pop("contract", None)


def _compile_spec(ref: IndicatorRef) -> StrategySpec:
    return StrategySpec(
        strategy_id="new-ind",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=ref))],
        exit_rules=[StopLossRule(pct=0.05)],
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["QQQ"],
    )


_COMPILE_REFS = [
    IndicatorRef(name="donchian", params={"band": "upper", "period": 20}),
    IndicatorRef(
        name="keltner", params={"band": "lower", "period": 20, "atr_period": 10, "multiplier": 1.5}
    ),
    IndicatorRef(name="obv"),
    IndicatorRef(name="mfi", params={"period": 14}),
    IndicatorRef(name="roc", params={"period": 10}),
    IndicatorRef(name="cci", params={"period": 20}),
    IndicatorRef(name="williams_r", params={"period": 14}),
    IndicatorRef(name="bollinger", params={"band": "percent_b", "period": 20}),
    IndicatorRef(name="bollinger", params={"band": "bandwidth", "period": 20}),
]


@pytest.mark.parametrize(
    "ref",
    _COMPILE_REFS,
    ids=[r.name + ":" + str(r.param("band") if "band" in r.params else "") for r in _COMPILE_REFS],
)
def test_compiles_and_inline_helper_matches_registry(ref, _contract_module) -> None:
    code = compile_strategy(_compile_spec(ref))
    assert "ctx.submit_order" not in code
    ns: dict = {}
    exec(compile(code, "<compiled>", "exec"), ns)  # noqa: S102 - compiling our own output
    strat = ns["CompiledStrategy"]()

    bars = _series(120, seed=5)
    method_map = {
        "donchian": "donchian_channels",
        "keltner": "keltner_channels",
        "bollinger": "bollinger_bands",
    }
    method = getattr(strat, method_map.get(ref.name, ref.name))
    reg = IndicatorRegistry()

    if ref.name == "donchian":
        compiled = method(bars, period=20, select="upper")
        expected = reg.donchian(bars, 20, "upper")
    elif ref.name == "keltner":
        compiled = method(bars, period=20, atr_period=10, multiplier=1.5, select="lower")
        expected = reg.keltner(bars, 20, 10, 1.5, "lower")
    elif ref.name == "bollinger":
        band = ref.param("band")
        compiled = method(bars, period=20, num_std=2.0, select=band)
        expected = reg.bollinger_bands(bars, 20, 2.0, select=band)
    elif ref.name == "obv":
        compiled, expected = method(bars), reg.obv(bars)
    elif ref.name == "roc":
        compiled, expected = method(bars, period=10), reg.roc(bars, 10)
    elif ref.name == "mfi":
        compiled, expected = method(bars, period=14), reg.mfi(bars, 14)
    elif ref.name == "cci":
        compiled, expected = method(bars, period=20), reg.cci(bars, 20)
    else:  # williams_r
        compiled, expected = method(bars, period=14), reg.williams_r(bars, 14)

    assert compiled == pytest.approx(expected, rel=0, abs=1e-9)


def test_conformance_fixture_synthesizes_for_new_indicator() -> None:
    """The conformance fixtures drive a new-indicator predicate through both states."""
    spec = StrategySpec(
        strategy_id="conf",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="williams_r", params={"period": 14}),
                    op="<",
                    rhs=-80.0,
                ),
            )
        ],
        exit_rules=[],
        target_symbols=["TEST"],
        requires_custom_code=True,
    )
    fixtures = generate_conformance_fixtures(spec)
    assert len(fixtures) == 1
    fx = fixtures[0]
    assert fx.synthesizable, fx.unsynthesizable_reason
    assert any(v is True for v in fx.expected_verdicts)
    assert any(v is False for v in fx.expected_verdicts)


_FAITHFUL_WILLIAMS = textwrap.dedent("""\
    class MyStrategy:
        UNIVERSE = frozenset({"TEST"})
        def on_bar(self, ctx, bar):
            if ctx.is_warmup:
                return
            if bar.symbol not in self.UNIVERSE:
                return
            wr = ctx.indicator("williams_r", period=14)
            if wr is None:
                return
            position = ctx.position(bar.symbol)
            if position is None and wr < -80:
                ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
""")

_DRIFTED_WILLIAMS = textwrap.dedent("""\
    class MyStrategy:
        UNIVERSE = frozenset({"TEST"})
        def on_bar(self, ctx, bar):
            if ctx.is_warmup:
                return
            if bar.symbol not in self.UNIVERSE:
                return
            wr = ctx.indicator("williams_r", period=14)
            if wr is None:
                return
            position = ctx.position(bar.symbol)
            # DRIFT: > instead of <
            if position is None and wr > -80:
                ctx.submit_order(symbol=bar.symbol, side="buy", qty=1.0)
""")


def _conf_spec(custom: bool = True) -> StrategySpec:
    return StrategySpec(
        strategy_id="conf-gate",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="williams_r", params={"period": 14}),
                    op="<",
                    rhs=-80.0,
                ),
            )
        ],
        exit_rules=[],
        target_symbols=["TEST"],
        requires_custom_code=custom,
    )


def test_conformance_gate_passes_faithful_williams() -> None:
    gate = PredicateConformanceGate()
    results = gate.check(_FAITHFUL_WILLIAMS, _conf_spec())
    assert any(r.passed for r in results)
    assert all(r.passed for r in results)


def test_conformance_gate_fails_drifted_williams() -> None:
    gate = PredicateConformanceGate()
    results = gate.check(_DRIFTED_WILLIAMS, _conf_spec())
    assert any(not r.passed for r in results)


# ---------------------------------------------------------------------------
# Pandas reference (executor/indicators.py) — series-shaped consumers + registry
# ---------------------------------------------------------------------------


def _frame():
    import pandas as pd

    bars = _series(80, seed=14)
    return (
        pd.Series([b.high for b in bars]),
        pd.Series([b.low for b in bars]),
        pd.Series([b.close for b in bars]),
        pd.Series([b.volume for b in bars]),
    )


def test_pandas_donchian_orders_bands() -> None:
    high, low, _close, _vol = _frame()
    upper, middle, lower = pdi.donchian_channels(high, low, period=20)
    mask = middle.notna()
    assert (upper[mask] >= middle[mask]).all()
    assert (middle[mask] >= lower[mask]).all()


def test_pandas_keltner_orders_bands() -> None:
    high, low, close, _vol = _frame()
    upper, middle, lower = pdi.keltner_channels(high, low, close, period=20, atr_period=10)
    mask = middle.notna() & upper.notna()
    assert (upper[mask] >= middle[mask]).all()
    assert (middle[mask] >= lower[mask]).all()


def test_pandas_obv_matches_registry_tail() -> None:
    _high, _low, close, vol = _frame()
    bars = _series(80, seed=14)
    assert float(pdi.obv(close, vol).iloc[-1]) == pytest.approx(IndicatorRegistry().obv(bars))


def test_pandas_mfi_bounded_and_matches_registry_tail() -> None:
    high, low, close, vol = _frame()
    series = pdi.mfi(high, low, close, vol, period=14)
    finite = series.dropna()
    assert (finite >= 0).all() and (finite <= 100).all()
    bars = _series(80, seed=14)
    assert float(series.iloc[-1]) == pytest.approx(IndicatorRegistry().mfi(bars, 14))


def test_pandas_roc_matches_registry_tail() -> None:
    _high, _low, close, _vol = _frame()
    bars = _series(80, seed=14)
    assert float(pdi.roc(close, period=12).iloc[-1]) == pytest.approx(
        IndicatorRegistry().roc(bars, 12)
    )


def test_pandas_cci_matches_registry_tail() -> None:
    high, low, close, _vol = _frame()
    bars = _series(80, seed=14)
    assert float(pdi.cci(high, low, close, period=20).iloc[-1]) == pytest.approx(
        IndicatorRegistry().cci(bars, 20)
    )


def test_pandas_williams_r_bounded_and_matches_registry_tail() -> None:
    high, low, close, _vol = _frame()
    series = pdi.williams_r(high, low, close, period=14)
    finite = series.dropna()
    assert (finite >= -100).all() and (finite <= 0).all()
    bars = _series(80, seed=14)
    assert float(series.iloc[-1]) == pytest.approx(IndicatorRegistry().williams_r(bars, 14))


@pytest.mark.parametrize(
    "name,arity",
    [
        ("donchian_channels", 3),
        ("keltner_channels", 3),
        ("obv", None),
        ("mfi", None),
        ("roc", None),
        ("cci", None),
        ("williams_r", None),
    ],
)
def test_indicators_registry_has_new_specs(name, arity) -> None:
    assert name in pdi.INDICATORS
    assert pdi.INDICATORS[name].tuple_arity == arity


# ---------------------------------------------------------------------------
# Engine series dispatch — compute_indicator_series routes through the
# streaming registry (the per-bar engine path) for every new IndicatorRef.
# ---------------------------------------------------------------------------


_SERIES_REFS = [
    IndicatorRef(name="donchian", params={"band": "upper", "period": 20}),
    IndicatorRef(name="donchian", params={"band": "middle", "period": 20}),
    IndicatorRef(
        name="keltner", params={"band": "lower", "period": 20, "atr_period": 10, "multiplier": 1.5}
    ),
    IndicatorRef(name="obv"),
    IndicatorRef(name="mfi", params={"period": 14}),
    IndicatorRef(name="roc", params={"period": 12}),
    IndicatorRef(name="cci", params={"period": 20}),
    IndicatorRef(name="williams_r", params={"period": 14}),
    IndicatorRef(name="bollinger", params={"band": "percent_b", "period": 20}),
    IndicatorRef(name="bollinger", params={"band": "bandwidth", "period": 20}),
]


@pytest.mark.parametrize("ref", _SERIES_REFS, ids=[r.sig_id for r in _SERIES_REFS])
def test_compute_indicator_series_matches_registry_tail(ref) -> None:
    """compute_indicator_series' trailing value equals a fresh registry's value."""
    import pandas as pd

    from investment_team.strategy_lab.executor.predicate_evaluator import (
        _registry_indicator,
        compute_indicator_series,
    )

    bars = _series(80, seed=23)
    df = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    series = compute_indicator_series(ref, df)
    expected = _registry_indicator(IndicatorRegistry(), ref, bars)
    assert float(series.iloc[-1]) == pytest.approx(expected, rel=0, abs=1e-9)
