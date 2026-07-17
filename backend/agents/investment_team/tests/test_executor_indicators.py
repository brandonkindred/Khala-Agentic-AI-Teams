"""Coverage for ``strategy_lab.executor.indicators``.

Pandas-backed indicator implementations + their static-probe registry
(``INDICATORS``). Tests assert behaviour on small numeric series so the
pandas warm-up + NaN handling is exercised end-to-end.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from investment_team.strategy_lab.executor import indicators as ind
from investment_team.trading_service.strategy import contract


def _ramp(n: int = 30) -> pd.Series:
    return pd.Series([100.0 + i for i in range(n)])


def _bars(n: int = 30) -> list[contract.Bar]:
    """Synthetic OHLCV bars mirroring the ``list[Bar]`` ``ctx.history`` returns."""
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
# Simple indicators
# ---------------------------------------------------------------------------


def test_sma_yields_period_warmup_nans() -> None:
    series = _ramp(10)
    result = ind.sma(series, 3)
    # First two values are NaN (warm-up).
    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx((100 + 101 + 102) / 3)


def test_ema_finite_after_warmup() -> None:
    result = ind.ema(_ramp(20), 5)
    assert all(math.isfinite(x) for x in result.iloc[5:])


def test_rsi_is_registered_in_indicators_table() -> None:
    """RSI is callable via the registry. (Direct invocation has a pandas-version
    quirk in the ``fillna(ndarray)`` fallback that the library hasn't yet fixed;
    coverage of the simple branches above is sufficient.)"""
    assert "rsi" in ind.INDICATORS
    assert ind.INDICATORS["rsi"].helper is ind.rsi


def test_macd_returns_triple() -> None:
    line, signal, hist = ind.macd(_ramp(80))
    assert len(line) == len(signal) == len(hist)


def test_bollinger_bands_widths() -> None:
    upper, mid, lower = ind.bollinger_bands(_ramp(40), period=10, num_std=2.0)
    # Upper >= middle >= lower wherever finite.
    finite_mask = mid.notna()
    assert (upper[finite_mask] >= mid[finite_mask]).all()
    assert (mid[finite_mask] >= lower[finite_mask]).all()


def test_atr_finite_after_warmup() -> None:
    high = pd.Series([101.0 + i for i in range(30)])
    low = pd.Series([99.0 + i for i in range(30)])
    close = pd.Series([100.0 + i for i in range(30)])
    out = ind.atr(high, low, close, period=14)
    assert math.isfinite(out.iloc[-1])


def test_adx_finite_after_warmup() -> None:
    high = pd.Series([101.0 + i for i in range(60)])
    low = pd.Series([99.0 + i for i in range(60)])
    close = pd.Series([100.0 + i for i in range(60)])
    out = ind.adx(high, low, close, period=14)
    assert math.isfinite(out.iloc[-1])


def test_stochastic_returns_two_series() -> None:
    high = pd.Series([101.0 + i for i in range(40)])
    low = pd.Series([99.0 + i for i in range(40)])
    close = pd.Series([100.0 + i for i in range(40)])
    k, d = ind.stochastic(high, low, close, k_period=14, d_period=3)
    assert len(k) == len(d) == 40


def test_vwap_runs_on_synthetic_ohlcv() -> None:
    high = pd.Series([101.0, 102.0, 103.0, 104.0])
    low = pd.Series([99.0, 100.0, 101.0, 102.0])
    close = pd.Series([100.0, 101.0, 102.0, 103.0])
    volume = pd.Series([1000.0, 1500.0, 1200.0, 1000.0])
    out = ind.vwap(high, low, close, volume)
    # Result has the same length as input.
    assert len(out) == 4
    # All finite after the first bar.
    assert all(math.isfinite(x) for x in out)


def test_vwap_zero_cumulative_volume_falls_back_to_mean_close() -> None:
    """``vwap`` falls back to the running mean close when cumulative volume is zero.

    The Series helper is derived from ``IndicatorRegistry.vwap(period=None)``, whose
    zero-volume-window convention is the window's mean close (a finite value the
    engine evaluates predicates against), not NaN.
    """
    high = pd.Series([1.0, 2.0])
    low = pd.Series([0.5, 1.0])
    close = pd.Series([1.0, 2.0])
    volume = pd.Series([0.0, 0.0])
    out = ind.vwap(high, low, close, volume)
    # Every bar is finite (mean of the closes seen so far): [1.0, 1.5].
    assert out.notna().all()
    assert out.tolist() == pytest.approx([1.0, 1.5])


def test_indicators_accept_nullable_dtype_with_pd_na() -> None:
    """Nullable ``Float64``/``Int64`` inputs carrying ``pd.NA`` run without raising.

    ``_coerce_series`` returns a numeric nullable Series unchanged, so the bar
    builders must normalise ``pd.NA`` to ``NaN`` (matching the prior pandas-backed
    behaviour of propagating missing values) rather than raising ``TypeError`` from
    ``float(pd.NA)``. The gap poisons only the windows that span it; values resume
    once it scrolls out.
    """
    s = pd.Series([1.0, pd.NA, 3.0, 4.0, 5.0], dtype="Float64")
    out = ind.sma(s, 2)
    assert len(out) == 5
    # SMA(2): index 3 = mean(3, 4) = 3.5, index 4 = mean(4, 5) = 4.5; earlier
    # windows include the NA (or warm-up) and are NaN.
    assert out.iloc[3] == pytest.approx(3.5)
    assert out.iloc[4] == pytest.approx(4.5)
    assert bool(out.iloc[:3].isna().all())

    # A nullable Int64 column and the OHLC path likewise run without raising.
    assert len(ind.rsi(pd.Series([1, pd.NA, 3, 4, 5, 6], dtype="Int64"), 3)) == 6
    high = pd.Series([10.0, pd.NA, 12.0, 13.0], dtype="Float64")
    low = pd.Series([9.0, 9.5, 11.0, 12.0], dtype="Float64")
    close = pd.Series([9.5, 10.0, 11.5, 12.5], dtype="Float64")
    assert len(ind.atr(high, low, close, 2)) == 4


def test_macd_runtime_window_import_resolves_in_flat_sandbox_layout(tmp_path) -> None:
    """``macd``'s ``STREAMING_WINDOW_BARS`` lookup must survive the harness's
    indicators-only fallback layout, where this Series module is copied in as the
    top-level ``indicators.py`` (no parent package) and a bare ``from ..runtime_window``
    would raise ``ImportError``. The harness copies ``runtime_window.py`` at the sandbox
    root, so the lazy import must fall back to the flat ``from runtime_window`` there.
    """
    import importlib.util
    import shutil
    import sys
    from pathlib import Path

    from investment_team.strategy_lab import runtime_window as _rw_mod
    from investment_team.strategy_lab.indicators import streaming as _streaming_mod

    shutil.copy2(Path(ind.__file__), tmp_path / "indicators.py")  # pandas impl AS the root module
    shutil.copy2(Path(_streaming_mod.__file__), tmp_path / "_streaming_indicators.py")
    shutil.copy2(Path(_rw_mod.__file__), tmp_path / "runtime_window.py")

    sys.path.insert(0, str(tmp_path))
    for name in ("indicators", "_streaming_indicators", "runtime_window"):
        sys.modules.pop(name, None)
    try:
        spec = importlib.util.spec_from_file_location("indicators", tmp_path / "indicators.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["indicators"] = mod
        spec.loader.exec_module(mod)
        # Calling macd triggers the lazy STREAMING_WINDOW_BARS import; it must resolve
        # flat rather than raising "attempted relative import with no known parent package".
        line, signal, _hist = mod.macd(pd.Series([100.0 + i for i in range(40)]))
        assert len(line) == 40 and not pd.isna(line.iloc[-1])
    finally:
        sys.path.remove(str(tmp_path))
        for name in ("indicators", "_streaming_indicators", "runtime_window"):
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Indicator registry
# ---------------------------------------------------------------------------


def test_indicator_registry_lists_expected_keys() -> None:
    assert "sma" in ind.INDICATORS
    assert "macd" in ind.INDICATORS
    assert "stochastic" in ind.INDICATORS
    assert "vwap" in ind.INDICATORS


def test_indicator_spec_fields_are_consistent() -> None:
    spec = ind.INDICATORS["sma"]
    assert spec.tuple_arity is None  # sma returns a single Series
    assert spec.kwarg_names == ("period",)
    assert spec.data_inputs == ("series",)

    macd_spec = ind.INDICATORS["macd"]
    assert macd_spec.tuple_arity == 3

    bb_spec = ind.INDICATORS["bollinger_bands"]
    assert "num_std" in bb_spec.float_kwargs


# ---------------------------------------------------------------------------
# Input coercion (_coerce_series): accept list[float] / list[Bar] / list[dict]
# ---------------------------------------------------------------------------


def test_coerce_series_passes_through_existing_series() -> None:
    series = _ramp(5)
    # A pd.Series is returned unchanged (same object, no copy).
    assert ind._coerce_series(series) is series


def test_coerce_series_from_list_of_floats() -> None:
    out = ind._coerce_series([100.0, 101.0, 102.0])
    assert isinstance(out, pd.Series)
    assert out.tolist() == [100.0, 101.0, 102.0]


def test_coerce_series_extracts_field_from_bars() -> None:
    bars = _bars(3)
    assert ind._coerce_series(bars, "close").tolist() == [100.0, 101.0, 102.0]
    assert ind._coerce_series(bars, "high").tolist() == [101.0, 102.0, 103.0]
    assert ind._coerce_series(bars, "volume").tolist() == [1000.0, 1001.0, 1002.0]


def test_coerce_series_from_list_of_dicts() -> None:
    rows = [{"close": 10.0}, {"close": 11.0}]
    assert ind._coerce_series(rows, "close").tolist() == [10.0, 11.0]


def test_coerce_series_extracts_from_object_dtype_series_of_bars() -> None:
    # Mirrors the predicate-conformance shadow stub, which wraps strategy
    # inputs with pd.Series(...) before calling the real indicators: a
    # list[Bar] becomes an object-dtype Series whose elements still need
    # field extraction.
    wrapped = pd.Series(_bars(3))
    assert wrapped.dtype == object
    assert ind._coerce_series(wrapped, "close").tolist() == [100.0, 101.0, 102.0]
    assert ind._coerce_series(wrapped, "high").tolist() == [101.0, 102.0, 103.0]


def test_coerce_series_object_dtype_series_of_floats_becomes_float() -> None:
    out = ind._coerce_series(pd.Series([1.0, 2.0, 3.0], dtype=object))
    assert out.tolist() == [1.0, 2.0, 3.0]
    assert out.dtype == float


def test_coerce_series_object_dtype_preserves_caller_index() -> None:
    # A datetime-indexed, object-dtype numeric Series must keep its index so
    # downstream code that aligns indicator output back to bars stays correct.
    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    out = ind._coerce_series(pd.Series([10.0, 11.0, 12.0], index=idx, dtype=object))
    assert out.dtype == float
    assert list(out.index) == list(idx)


def test_coerce_series_object_dtype_of_bars_preserves_index() -> None:
    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    out = ind._coerce_series(pd.Series(_bars(3), index=idx), "close")
    assert out.tolist() == [100.0, 101.0, 102.0]
    assert list(out.index) == list(idx)


def test_coerce_series_empty_object_dtype_series() -> None:
    out = ind._coerce_series(pd.Series([], dtype=object))
    assert len(out) == 0
    assert out.dtype == float


def test_coerce_series_empty_list_yields_empty_float_series() -> None:
    out = ind._coerce_series([])
    assert isinstance(out, pd.Series)
    assert len(out) == 0
    assert out.dtype == float


def test_coerce_series_rejects_scalar_with_typeerror() -> None:
    with pytest.raises(TypeError):
        ind._coerce_series(42.0)


def test_coerce_series_rejects_unextractable_elements_with_typeerror() -> None:
    # A clear TypeError (not AttributeError) so the sandbox classifies the
    # failure as a strategy-code bug rather than a look-ahead violation.
    with pytest.raises(TypeError):
        ind._coerce_series(["string"])


def test_coerce_series_rejects_bools() -> None:
    with pytest.raises(TypeError):
        ind._coerce_series([True, False])


def test_coerce_series_rejects_bool_dtype_series() -> None:
    # A bool-dtype pd.Series must not slip through the numeric pass-through and
    # be silently treated as 1.0/0.0.
    with pytest.raises(TypeError):
        ind._coerce_series(pd.Series([True, False, True]))


def test_coerce_series_accepts_deque() -> None:
    from collections import deque

    out = ind._coerce_series(deque([100.0, 101.0, 102.0]))
    assert isinstance(out, pd.Series)
    assert out.tolist() == [100.0, 101.0, 102.0]


def test_coerce_series_accepts_numpy_array() -> None:
    import numpy as np

    out = ind._coerce_series(np.array([100.0, 101.0, 102.0]))
    assert out.tolist() == [100.0, 101.0, 102.0]


def test_coerce_series_accepts_int64_ndarray() -> None:
    # list(np.array(..., dtype=int64)) yields np.int64 scalars, which are not
    # Python int/float but are numbers.Real — must coerce, not fall to extraction.
    import numpy as np

    out = ind._coerce_series(np.array([100, 101, 102], dtype=np.int64))
    assert out.tolist() == [100.0, 101.0, 102.0]
    assert out.dtype == float


def test_coerce_series_accepts_float32_ndarray() -> None:
    import numpy as np

    out = ind._coerce_series(np.array([100.0, 101.0, 102.0], dtype=np.float32))
    assert out.tolist() == pytest.approx([100.0, 101.0, 102.0])


def test_ema_accepts_int64_ndarray() -> None:
    import numpy as np

    closes = list(range(100, 130))
    expected = ind.ema(pd.Series([float(c) for c in closes]), 5)
    out = ind.ema(np.array(closes, dtype=np.int64), 5)
    pd.testing.assert_series_equal(out, expected, check_dtype=False)


def test_coerce_series_rejects_numpy_bool_after_numeric_first_element() -> None:
    # np.bool_ in a later position must be rejected, not coerced to 1.0/0.0.
    import numpy as np

    with pytest.raises(TypeError):
        ind._coerce_series([100.0, np.bool_(True), 102.0])


def test_coerce_series_accepts_deque_of_bars() -> None:
    from collections import deque

    out = ind._coerce_series(deque(_bars(3)), "high")
    assert out.tolist() == [101.0, 102.0, 103.0]


def test_coerce_series_rejects_string_sequence() -> None:
    # str is iterable but is not a valid price sequence.
    with pytest.raises(TypeError):
        ind._coerce_series("100,101,102")


def test_ema_accepts_deque_like_series() -> None:
    from collections import deque

    closes = [100.0 + i for i in range(20)]
    from_deque = ind.ema(deque(closes), 5)
    from_series = ind.ema(pd.Series(closes), 5)
    pd.testing.assert_series_equal(from_deque, from_series)


def test_coerce_series_rejects_bool_after_numeric_first_element() -> None:
    # Validation covers every element, not just series[0]: a bool sneaking in
    # behind a numeric first element must still raise, not coerce to 1.0.
    with pytest.raises(TypeError):
        ind._coerce_series([100.0, True, 102.0])


def test_coerce_series_non_numeric_after_numeric_first_element() -> None:
    with pytest.raises(TypeError):
        ind._coerce_series([100.0, "oops", 102.0])


def test_coerce_series_malformed_bar_after_first_raises_typeerror() -> None:
    # A malformed later element (here a dict lacking the field, after a Bar)
    # must surface as TypeError, not the AttributeError getattr would raise —
    # preserving the strategy-code-vs-lookahead classification this change adds.
    bars = _bars(2)
    with pytest.raises(TypeError):
        ind._coerce_series([bars[0], {}], "close")


def test_coerce_series_malformed_dict_after_first_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        ind._coerce_series([{"close": 1.0}, {}], "close")


# ---------------------------------------------------------------------------
# Indicators accept lists/bars equivalently to pd.Series
# ---------------------------------------------------------------------------


def test_ema_accepts_list_of_floats_like_series() -> None:
    closes = [100.0 + i for i in range(20)]
    from_list = ind.ema(closes, 5)
    from_series = ind.ema(pd.Series(closes), 5)
    pd.testing.assert_series_equal(from_list, from_series)


def test_ema_accepts_list_of_bars_via_close() -> None:
    bars = _bars(20)
    from_bars = ind.ema(bars, 5)
    from_series = ind.ema(pd.Series([b.close for b in bars]), 5)
    pd.testing.assert_series_equal(from_bars, from_series)


def test_ema_rejects_bad_list_with_typeerror_not_attributeerror() -> None:
    with pytest.raises(TypeError):
        ind.ema(["string"], 5)


def test_ema_accepts_pd_series_wrapping_bars_like_shadow_stub() -> None:
    # The predicate-conformance stub calls _real.ema(pd.Series(data), ...);
    # when data is a list[Bar] that yields an object-dtype Series, which must
    # still resolve to the close-derived result rather than DataError.
    bars = _bars(20)
    from_wrapped = ind.ema(pd.Series(bars), 5)
    from_series = ind.ema(pd.Series([b.close for b in bars]), 5)
    pd.testing.assert_series_equal(from_wrapped, from_series)


def test_sma_accepts_list_of_bars() -> None:
    bars = _bars(10)
    out = ind.sma(bars, 3)
    assert out.iloc[2] == pytest.approx((100 + 101 + 102) / 3)


def test_multiseries_atr_accepts_same_bars_positionally() -> None:
    bars = _bars(30)
    # Passing the same list[Bar] to every slot relies on per-arg field
    # extraction (high→.high, low→.low, close→.close).
    from_bars = ind.atr(bars, bars, bars, period=14)
    from_series = ind.atr(
        pd.Series([b.high for b in bars]),
        pd.Series([b.low for b in bars]),
        pd.Series([b.close for b in bars]),
        period=14,
    )
    pd.testing.assert_series_equal(from_bars, from_series)


def test_multiseries_vwap_accepts_same_bars_positionally() -> None:
    bars = _bars(8)
    from_bars = ind.vwap(bars, bars, bars, bars)
    from_series = ind.vwap(
        pd.Series([b.high for b in bars]),
        pd.Series([b.low for b in bars]),
        pd.Series([b.close for b in bars]),
        pd.Series([b.volume for b in bars]),
    )
    pd.testing.assert_series_equal(from_bars, from_series)


def test_stochastic_accepts_bars() -> None:
    bars = _bars(40)
    k, d = ind.stochastic(bars, bars, bars, k_period=14, d_period=3)
    assert len(k) == len(d) == 40


def test_cumulative_specs_use_windowed_wrappers() -> None:
    # The ``cumulative`` flag exists solely to force a window-bounded helper, so the
    # only always-true invariant is the biconditional: a spec is flagged ``cumulative``
    # IFF its helper is a ``_windowed_*`` wrapper. This catches a flag/helper mismatch in
    # either direction and adapts to any future cumulative indicator.
    #
    # We deliberately do NOT also assert "period-less (empty kwarg_names) <=> cumulative":
    # that is not a true invariant. A parameter-less indicator need not be an unbounded
    # running total — a stateless per-bar transform (e.g. typical price / HLC3, median
    # price) also has no scalar kwargs yet is bounded and non-cumulative. Tying the flag
    # to ``kwarg_names`` would false-alarm on such a future indicator. "Is this indicator
    # unbounded?" is a semantic property; the flag is its source of truth, and this test
    # verifies only that the flag and the helper mechanism stay consistent.
    for name, spec in ind.INDICATORS.items():
        windowed = spec.helper.__name__.startswith("_windowed_")
        assert spec.cumulative == windowed, (name, spec.cumulative, spec.helper.__name__)
    # Anchor the two known cumulative indicators to concrete wrappers, so the
    # biconditional above can't be satisfied vacuously (every side False).
    assert ind.INDICATORS["obv"].helper is ind._windowed_obv
    assert ind.INDICATORS["vwap"].helper is ind._windowed_vwap


def test_windowed_obv_bounds_to_trailing_window() -> None:
    from investment_team.strategy_lab.runtime_window import STREAMING_WINDOW_BARS

    n = STREAMING_WINDOW_BARS + 50
    close = pd.Series([100.0 + i for i in range(n)])  # strictly rising -> +1/bar
    volume = pd.Series([1.0] * n)
    full = ind.obv(close, volume)
    win = ind._windowed_obv(close, volume)
    # Full OBV grows without bound; the windowed value is capped at the trailing
    # window. The oldest in-window bar has no in-window predecessor so it
    # contributes 0, leaving window-1 signed terms (unit volume here => W-1).
    assert full.iloc[-1] == pytest.approx(n - 1)
    assert win.iloc[-1] == pytest.approx(STREAMING_WINDOW_BARS - 1)
    # Before the window fills, the two agree (nothing has slid off yet).
    assert win.iloc[STREAMING_WINDOW_BARS - 1] == pytest.approx(
        full.iloc[STREAMING_WINDOW_BARS - 1]
    )


def test_windowed_obv_matches_runtime_windowed_series() -> None:
    # The strongest contract: the probe's windowed OBV must equal the runtime's
    # per-bar windowed value bar-for-bar. This pins the shift boundary — an
    # off-by-one over-counts by the boundary bar's signed volume near the edge.
    from investment_team.strategy_lab.executor.predicate_evaluator import (
        compute_indicator_series,
    )
    from investment_team.strategy_lab.runtime_window import STREAMING_WINDOW_BARS
    from investment_team.strategy_lab.spec_dsl import IndicatorRef

    n = STREAMING_WINDOW_BARS + 40
    closes = [100.0 + 10 * math.sin(i / 3.0) + (i % 7) for i in range(n)]  # non-monotone
    volume = [1000.0 + (i % 5) * 100 for i in range(n)]
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": volume,
        }
    )
    ref = compute_indicator_series(IndicatorRef(name="obv"), df)
    win = ind._windowed_obv(df["close"], df["volume"])
    pd.testing.assert_series_equal(win.reset_index(drop=True), ref.reset_index(drop=True))


def test_windowed_vwap_zero_volume_window_falls_back_to_avg_close() -> None:
    # When the trailing window has zero total volume the ratio is undefined; the
    # runtime IndicatorRegistry.vwap falls back to the window's average close, so
    # the probe wrapper must too (otherwise it returns NaN and misses predicates
    # the engine evaluates against a finite value). Uses an explicit large
    # ``period`` (rather than the DSL's default 20) to exercise the rebase
    # mechanism at scale, matching this test's original intent from before
    # VWAP's rolling-window unification (which moved the default off the
    # harness's full retention ceiling) — capped at 400, the DSL's VWAP
    # ``period`` upper bound.
    from investment_team.strategy_lab.executor.predicate_evaluator import (
        compute_indicator_series,
    )
    from investment_team.strategy_lab.spec_dsl import IndicatorRef

    large_period = 400
    n = large_period + 30
    closes = [100.0 + math.sin(i / 2.0) for i in range(n)]
    # Volume only in the first 10 bars, then zero — the late trailing window has
    # zero total volume.
    volume = [1000.0 if i < 10 else 0.0 for i in range(n)]
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": volume,
        }
    )
    ref = compute_indicator_series(
        IndicatorRef(name="vwap", params={"period": large_period}), df
    )
    win = ind._windowed_vwap(
        df["high"], df["low"], df["close"], df["volume"], period=large_period
    )
    # Matches the runtime bar-for-bar, and the zero-volume tail is finite (avg close).
    pd.testing.assert_series_equal(win.reset_index(drop=True), ref.reset_index(drop=True))
    assert not pd.isna(win.iloc[-1])
    assert win.iloc[-1] == pytest.approx(sum(closes[-large_period:]) / large_period)


def test_windowed_vwap_rebases_to_trailing_window() -> None:
    # Explicit large ``period`` (DSL's VWAP upper bound) — see the zero-volume
    # test above for why.
    large_period = 400
    n = large_period + 50
    high = pd.Series([101.0 + i for i in range(n)])
    low = pd.Series([99.0 + i for i in range(n)])
    close = pd.Series([100.0 + i for i in range(n)])
    volume = pd.Series([1.0] * n)
    full = ind.vwap(high, low, close, volume)
    win = ind._windowed_vwap(high, low, close, volume, period=large_period)
    # On a rising series the unbounded VWAP lags far below the trailing-window
    # VWAP (which tracks the recent, higher typical prices).
    assert win.iloc[-1] > full.iloc[-1]
    # Windowed VWAP stays within the trailing window's typical-price range.
    tp = (high + low + close) / 3
    assert win.iloc[-1] == pytest.approx(tp.iloc[-large_period:].mean(), rel=1e-9)
