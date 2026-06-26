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
