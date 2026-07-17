"""Coverage for ``strategy_lab.executor.strategy_indicators``.

The scalar-returning indicator API exposed to strategy ``on_bar`` code. It must
(1) return the latest scalar value of the corresponding Series indicator,
(2) accept the same input shapes as the underlying executor helpers
(``list[Bar]``, ``list[float]``, ``deque``, …), and (3) be importable both
in-package (predicate-conformance gate) and in the flat sandbox layout where
the Series implementation is named ``_indicators_impl``.
"""

from __future__ import annotations

import importlib.util
import math
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

from investment_team.strategy_lab.executor import indicators as ind
from investment_team.strategy_lab.executor import strategy_indicators as si
from investment_team.trading_service.strategy import contract


def _bars(n: int = 30) -> list[contract.Bar]:
    return [
        contract.Bar(
            symbol="TEST",
            timestamp=f"2024-01-{(i % 28) + 1:02d}T00:00:00Z",
            timeframe="1d",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1000.0 + i,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Scalar contract: helpers return the last value of the Series indicator
# ---------------------------------------------------------------------------


def test_single_output_helpers_return_last_scalar() -> None:
    """The scalar helpers return the streaming registry's trailing value (the
    engine's authoritative indicator math), not the legacy pandas Series value
    — EMA/RSI use the windowed recurrences, which differ from pandas ewm."""
    from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry

    bars = _bars(40)
    assert si.sma(bars, 5) == pytest.approx(IndicatorRegistry().sma(bars, 5))
    assert si.ema(bars, 5) == pytest.approx(IndicatorRegistry().ema(bars, 5))
    assert si.rsi(bars, 14) == pytest.approx(IndicatorRegistry().rsi(bars, 14))
    assert isinstance(si.sma(bars, 5), float)


def test_multi_output_helpers_return_tuples_of_floats() -> None:
    bars = _bars(80)
    macd_line, signal, hist = si.macd(bars)
    assert all(isinstance(v, float) for v in (macd_line, signal, hist))
    upper, middle, lower = si.bollinger_bands(bars, 20)
    assert upper >= middle >= lower
    k, d = si.stochastic(bars, bars, bars)
    assert all(isinstance(v, float) for v in (k, d))


def test_multi_series_helpers_accept_repeated_history() -> None:
    bars = _bars(40)
    assert si.atr(bars, bars, bars, period=14) == pytest.approx(
        ind.atr(
            pd.Series([b.high for b in bars]),
            pd.Series([b.low for b in bars]),
            pd.Series([b.close for b in bars]),
            period=14,
        ).iloc[-1]
    )
    assert isinstance(si.vwap(bars, bars, bars, bars), float)
    assert isinstance(si.adx(bars, bars, bars, 14), float)


def test_helpers_never_return_series() -> None:
    bars = _bars(30)
    for value in (si.sma(bars, 5), si.ema(bars, 5), si.rsi(bars, 14), si.atr(bars, bars, bars)):
        assert not isinstance(value, pd.Series)


def test_warmup_returns_zero_not_nan() -> None:
    # Too few bars for the window → underlying Series is all-NaN → 0.0.
    assert si.sma(_bars(2), 20) == 0.0
    assert si.ema(_bars(0) or [], 20) == 0.0


def test_helpers_accept_list_float_and_deque() -> None:
    from collections import deque

    closes = [100.0 + i for i in range(20)]
    assert si.ema(closes, 5) == pytest.approx(si.ema(deque(closes), 5))


# ---------------------------------------------------------------------------
# Shared registry (issue: backtest hot loop caching) — input-shape handling
# ---------------------------------------------------------------------------


def test_shared_registry_accepts_raw_pandas_series() -> None:
    """The shared-registry symbol-peek must use ``.iloc[-1]`` (positional) for
    a pandas ``Series``, not ``[-1]`` (label lookup, which can raise/misbehave
    on a non-default index). A raw ``Series`` is a shape ``_coerce_series``
    documents accepting but no other test passes directly into this module."""
    s = pd.Series([100.0 + i for i in range(20)])
    assert si.ema(s, 5) == pytest.approx(si.ema(list(s), 5))


def test_shared_registry_does_not_consume_a_generator() -> None:
    """Regression test: a naive symbol-peek (``bars[-1]``/``len(bars)`` on an
    arbitrary object) would crash on, or silently exhaust, a one-shot
    generator before the wrapper's own ``_coerce_series`` call gets to consume
    it. The shared registry must leave non-indexable input un-peeked."""

    def _gen():
        yield from (100.0 + i for i in range(20))

    assert si.ema(_gen(), 5) == pytest.approx(si.ema([100.0 + i for i in range(20)], 5))


def test_regbar_helpers_propagate_timestamp_from_bar_like_input() -> None:
    """Deterministic proof of the mechanism behind ``_shared_registry``'s
    safety: once a registry is shared across calls, IndicatorRegistry's own
    fingerprinting needs a real per-bar timestamp to tell a genuinely new bar
    window apart from a coincidentally-similar one (same length, same
    trailing close, possibly-reused ``id()``) — this asserts each ``_RegBar``
    built from real ``Bar`` objects carries that bar's actual timestamp
    rather than the ``None`` it used to hard-code."""
    bars = _bars(10)
    value_bars = si._value_bars(bars)
    assert [b.timestamp for b in value_bars] == [b.timestamp for b in bars]
    ohlc_bars = si._ohlc_bars(bars, bars, bars)
    assert [b.timestamp for b in ohlc_bars] == [b.timestamp for b in bars]
    from_history = si._ohlc_bars_from_history(bars)
    assert [b.timestamp for b in from_history] == [b.timestamp for b in bars]


def test_shared_registry_skips_cache_when_no_timestamp_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch: with no timestamp derivable (plain floats), sharing
    would let IndicatorRegistry's coincidental close-value fallback (see
    ``_shared_registry``'s docstring) merge two unrelated calls. Rather than
    risk that, ``_shared_registry`` falls back to a fresh, uncached instance
    every call — proven here by asserting the constructor runs once per call,
    not once total, for plain-number input with no stable per-bar signal."""
    from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry

    constructed: list[object] = []
    real_init = IndicatorRegistry.__init__

    def _counting_init(self) -> None:
        constructed.append(self)
        real_init(self)

    monkeypatch.setattr(IndicatorRegistry, "__init__", _counting_init)

    closes = [100.0 + i for i in range(20)]
    si.ema(closes, 5)
    si.ema(closes, 5)
    assert len(constructed) == 2


# ---------------------------------------------------------------------------
# Flat sandbox layout: the module imports the impl as ``_indicators_impl``
# ---------------------------------------------------------------------------


def test_module_loads_in_flat_sandbox_layout(tmp_path: Path) -> None:
    """Replicates what the streaming harness copies into the sandbox: the
    Series impl as ``_indicators_impl.py``, the scalar wrapper as
    ``indicators.py``, and the registry as ``_streaming_indicators.py``, with
    only the temp dir on ``sys.path``."""
    from investment_team.strategy_lab.indicators import streaming as _streaming_mod

    impl_src = Path(ind.__file__)
    scalar_src = Path(si.__file__)
    registry_src = Path(_streaming_mod.__file__)
    shutil.copy2(impl_src, tmp_path / "_indicators_impl.py")
    shutil.copy2(scalar_src, tmp_path / "indicators.py")
    shutil.copy2(registry_src, tmp_path / "_streaming_indicators.py")

    sys.path.insert(0, str(tmp_path))
    # Ensure a clean import of the sandbox copy, not the in-package module.
    for name in ("indicators", "_indicators_impl", "_streaming_indicators"):
        sys.modules.pop(name, None)
    try:
        spec = importlib.util.spec_from_file_location("indicators", tmp_path / "indicators.py")
        sandbox_ind = importlib.util.module_from_spec(spec)
        sys.modules["indicators"] = sandbox_ind
        spec.loader.exec_module(sandbox_ind)

        closes = [100.0 + i for i in range(20)]
        assert sandbox_ind.ema(closes, 5) == pytest.approx(si.ema(closes, 5))
        assert not isinstance(sandbox_ind.ema(closes, 5), pd.Series)
        assert math.isfinite(sandbox_ind.ema(closes, 5))
    finally:
        sys.path.remove(str(tmp_path))
        for name in ("indicators", "_indicators_impl", "_streaming_indicators"):
            sys.modules.pop(name, None)
