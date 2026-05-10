"""Conjunction-coverage tests for the indicator-coverage probe (#448)."""

from __future__ import annotations

import textwrap

import numpy as np
import pandas as pd

from investment_team.models import CoverageCategory
from investment_team.strategy_lab.coverage_probe import run_indicator_probe


def _swing_ohlcv(n: int = 200, leg: int = 50, step: float = 0.005) -> pd.DataFrame:
    """Sawtooth price series that drives RSI to its extremes."""
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


def test_conjunction_never_true_when_individual_subconditions_fire() -> None:
    # Each leg of ``close > sma`` and ``close < sma`` fires on roughly half
    # the bars of the swing fixture, but the bar-wise conjunction is
    # mathematically empty.
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > sma(close, 5) and close < sma(close, 5):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )

    assert report.coverage_category is CoverageCategory.CONJUNCTION_NEVER_TRUE
    assert len(report.subconditions) == 2
    for sc in report.subconditions:
        assert sc.hit_count > 0
        assert sc.hit_rate > 0.0
    assert len(report.likely_blockers) == 1
    assert report.likely_blockers[0].reason == "conjunction_never_true"


def test_conjunction_partially_true_returns_coverage_ok() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and close > sma(close, 5):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _swing_ohlcv()},
    )

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 2
    assert report.likely_blockers == []


def test_nested_if_inherits_ancestor_predicate() -> None:
    """A nested ``if`` is reachable only when its ancestor's predicate
    is true, so the effective entry conjunction must include the
    ancestor. ``if close > 100: if close < 50: pass`` is unreachable
    even though each leg fires on different bars of the data.

    Without ancestor inheritance the probe would see two independent
    groups (one per ``if``), each individually firing, and report
    ``COVERAGE_OK``. With inheritance it sees a single group of
    ``[close > 100, close < 50]`` whose bar-wise AND is empty —
    ``CONJUNCTION_NEVER_TRUE``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 100:
                    if close < 50:
                        pass
        """
    )
    df = pd.DataFrame(
        {
            "open": [120.0] * 10 + [40.0] * 10,
            "high": [121.0] * 10 + [41.0] * 10,
            "low": [119.0] * 10 + [39.0] * 10,
            "close": [120.0] * 10 + [40.0] * 10,
            "volume": [1_000_000.0] * 20,
        }
    )
    report = run_indicator_probe(strategy_code=code, market_data={"SYM": df})

    assert report.coverage_category is CoverageCategory.CONJUNCTION_NEVER_TRUE
    # Both subconds have non-zero individual hit rates over the data.
    labels = {sc.label for sc in report.subconditions}
    assert "close > 100" in labels
    assert "close < 50" in labels
    for sc in report.subconditions:
        assert sc.hit_count > 0


def test_unrelated_if_branches_are_not_conjoined() -> None:
    """Two separate ``if`` predicates must not be ANDed together.

    ``if close > 100: enter`` and ``if close < 50: exit`` are independent
    branches. Their bar-wise AND is empty by design, but that is not a
    coverage problem — each branch fires in its own region. The probe
    must classify this as ``COVERAGE_OK`` (or ``INDICATOR_FILTER_TOO_RESTRICTIVE``
    if a leg never fires), never ``CONJUNCTION_NEVER_TRUE``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 100:
                    pass
                if close < 200:
                    pass
        """
    )
    df = pd.DataFrame(
        {
            "open": [120.0] * 10 + [180.0] * 10,
            "high": [121.0] * 10 + [181.0] * 10,
            "low": [119.0] * 10 + [179.0] * 10,
            "close": [120.0] * 10 + [180.0] * 10,
            "volume": [1_000_000.0] * 20,
        }
    )
    report = run_indicator_probe(strategy_code=code, market_data={"SYM": df})

    # close > 100 fires on every bar; close < 200 fires on every bar.
    # Each individual subcondition has hit_rate == 1.0 — but their
    # bar-wise AND would only be relevant if they shared a predicate,
    # which they don't.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 2
    for sc in report.subconditions:
        assert sc.hit_rate == 1.0


def test_conjunction_uses_intersection_not_product() -> None:
    """Ensures the conjunction hit-count equals bar-wise AND, not (rate_a * rate_b)."""
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 99 and close < 101:
                    pass
        """
    )
    df = pd.DataFrame(
        {
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.0] * 10,
            "volume": [1_000_000.0] * 10,
        }
    )
    report = run_indicator_probe(strategy_code=code, market_data={"SYM": df})

    # Every bar satisfies both halves so conjunction is truthful — not 0.5*0.5.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 2
    assert all(sc.hit_rate == 1.0 for sc in report.subconditions)
