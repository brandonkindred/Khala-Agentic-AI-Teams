"""Basic per-subcondition tests for the indicator-coverage probe (#448)."""

from __future__ import annotations

import textwrap

import numpy as np
import pandas as pd

from investment_team.models import CoverageCategory
from investment_team.strategy_lab.coverage_probe import run_indicator_probe


def _flat_ohlcv(n: int = 60, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": np.full(n, base),
            "high": np.full(n, base + 1.0),
            "low": np.full(n, base - 1.0),
            "close": np.full(n, base),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _swing_ohlcv(n: int = 200, leg: int = 50, step: float = 0.005) -> pd.DataFrame:
    """Sawtooth price series that drives RSI to its extremes.

    50 bars at -0.5%/bar take RSI well below 30; 50 bars at +0.5%/bar
    take it above 70. Two full cycles in 200 bars give every RSI
    threshold in (0..100) at least one strict crossing per cycle.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves: list[float] = []
    while len(moves) < n:
        moves.extend([-step] * leg)
        moves.extend([+step] * leg)
    moves = moves[:n]
    close = 100.0 * np.cumprod(1.0 + np.array(moves))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def test_always_true_subcondition_returns_coverage_ok() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    sc = report.subconditions[0]
    assert sc.hit_rate == 1.0
    assert sc.hit_count == 60
    assert sc.last_true_bar is not None
    assert report.bars_checked == 60


def test_never_true_subcondition_returns_too_restrictive() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close < -50:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )

    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count == 0
    assert report.subconditions[0].hit_rate == 0.0
    assert len(report.likely_blockers) == 1
    blocker = report.likely_blockers[0]
    assert blocker.reason == "indicator_filter_zero_hits"
    assert blocker.hit_rate == 0.0


def test_partial_fire_subcondition_populates_last_true_bar() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close < sma(close, 5):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    sc = report.subconditions[0]
    assert 0.0 < sc.hit_rate < 1.0
    assert sc.hit_count > 0
    assert sc.last_true_bar is not None


def test_insufficient_bars_short_circuits() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv(n=5)},
        warmup_bars_required=200,
    )

    assert report.coverage_category is CoverageCategory.INSUFFICIENT_BARS
    assert report.subconditions == []
    assert report.bars_checked == 5
    assert report.warmup_bars_required == 200
    assert len(report.likely_blockers) == 1
    assert report.likely_blockers[0].reason == "insufficient_bars"


def test_computed_indicator_variable_is_bound() -> None:
    """Strategy follows the standard ideation_system.md template:
    compute the indicator into a local then compare in the entry test.

    Without Name → indicator binding the RHS is a Name with no numeric
    constant binding, so the comparison gets dropped and the probe
    returns UNKNOWN_LOW_COVERAGE — silently masking real coverage.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                sma_var = sma(close, 5)
                if close > sma_var:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    sc = report.subconditions[0]
    assert sc.hit_count > 0


def test_bollinger_bands_subscript_recognized() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > bollinger_bands(close, 20)[0]:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )
    # Some bars in the swing fixture exceed the upper band; the
    # subcondition is recognised and partially fires.
    assert len(report.subconditions) == 1
    assert report.coverage_category in {
        CoverageCategory.COVERAGE_OK,
        CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
    }


def test_macd_subscript_recognized() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if macd(close)[0] > 0:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label.startswith("macd(close)[0] >")


def test_stochastic_subscript_recognized() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if stochastic(high, low, close)[0] < 20:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )
    assert len(report.subconditions) == 1


def test_vwap_recognized() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > vwap(high, low, close, volume):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )
    assert len(report.subconditions) == 1


def test_bollinger_bands_extra_positional_args_are_forwarded() -> None:
    """``bollinger_bands(close, 20, 0.1)[0]`` must use num_std=0.1, not 2.0.

    Regression for a bug where the tuple-indicator path forwarded only
    the period and silently dropped trailing args, so the probe computed
    against helper defaults rather than the strategy's actual thresholds.

    A tight ``num_std=0.1`` upper band is much closer to the SMA than the
    default 2.0 band, so ``close > upper`` fires substantially more often.
    Identical hit counts between the two forms would prove the extra arg
    is being dropped.
    """
    code_tight = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > bollinger_bands(close, 20, 0.1)[0]:
                    pass
        """
    )
    code_default = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > bollinger_bands(close, 20)[0]:
                    pass
        """
    )
    df = _swing_ohlcv()
    tight = run_indicator_probe(strategy_code=code_tight, market_data={"AAPL": df})
    default = run_indicator_probe(strategy_code=code_default, market_data={"AAPL": df})

    assert tight.subconditions[0].hit_count != default.subconditions[0].hit_count


def test_bollinger_bands_kwarg_num_std_is_forwarded() -> None:
    """The helper-specific kwarg form is recognised too."""
    code_kw = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > bollinger_bands(close, 20, num_std=0.1)[0]:
                    pass
        """
    )
    code_default = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > bollinger_bands(close, 20)[0]:
                    pass
        """
    )
    df = _swing_ohlcv()
    kw = run_indicator_probe(strategy_code=code_kw, market_data={"AAPL": df})
    default = run_indicator_probe(strategy_code=code_default, market_data={"AAPL": df})

    assert kw.subconditions[0].hit_count != default.subconditions[0].hit_count


def test_macd_extra_positional_args_are_forwarded() -> None:
    """``macd(close, 5, 10, 4)[0]`` must use those spans, not the defaults
    (12, 26, 9). Hit counts of close vs. the macd line differ by enough
    to prove the values flowed through to the helper.
    """
    code_short = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if macd(close, 5, 10, 4)[0] > 0:
                    pass
        """
    )
    code_default = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if macd(close)[0] > 0:
                    pass
        """
    )
    df = _swing_ohlcv()
    short = run_indicator_probe(strategy_code=code_short, market_data={"AAPL": df})
    default = run_indicator_probe(strategy_code=code_default, market_data={"AAPL": df})

    assert short.subconditions[0].hit_count != default.subconditions[0].hit_count


def test_volume_scaled_subcondition_recognized() -> None:
    df = _flat_ohlcv()
    df.loc[df.index[40:], "volume"] = 2_000_000.0  # half the bars at 2x baseline
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if volume > volume * 1.5:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"SYM": df},
    )

    # ``volume > volume * 1.5`` is structurally a never-true subcondition;
    # the probe should classify it as too-restrictive.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_rate == 0.0
