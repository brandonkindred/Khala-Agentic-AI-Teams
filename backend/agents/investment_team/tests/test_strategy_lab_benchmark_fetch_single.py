"""Regression: the SPY/AGG benchmark fetch is memoized per design attempt.

``_build_benchmark_equity`` used to fetch benchmark bars on every call. The
walk-forward/regime evaluation path it feeds into can run more than once
within a single design attempt, which used to re-issue the identical fetch
each time. This locks in that repeated evaluations within one attempt share
a single fetch (via ``_cached_fetch_benchmark_bars``'s memo), and that
resetting the memo (mirroring the per-attempt reset in
``_run_design_attempt``) correctly triggers exactly one more fetch.
"""

from __future__ import annotations

from investment_team.models import BacktestResult

from ._walk_forward_test_helpers import (
    StubMarketDataService,
    orchestrator,
    spec,
    stub_bars,
    trades_across_year,
)
from ._walk_forward_test_helpers import config as _config


def _base_metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=12.0,
        volatility_pct=8.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=4.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def test_benchmark_fetch_memoized_across_repeated_evaluations_in_one_attempt():
    """Calling the walk-forward evaluation path multiple times within the
    same attempt (no cache reset in between) issues only one SPY/AGG fetch,
    and every call returns an identical regime result."""
    stub = StubMarketDataService()
    orch = orchestrator(stub)
    config = _config(walk_forward_enabled=True, n_folds=5, benchmark_composition="60_40")
    trades = trades_across_year(n_per_month=4)
    market_data = {"AAPL": stub_bars("AAPL")}

    results = [
        orch._evaluate_walk_forward(spec(), market_data, config, trades, _base_metrics())
        for _ in range(3)
    ]

    benchmark_calls = [c for c in stub.calls if set(c["symbols"]) >= {"SPY", "AGG"}]
    assert len(benchmark_calls) == 1
    assert all(r.regime_results == results[0].regime_results for r in results)


def test_benchmark_fetch_refetches_after_per_attempt_cache_reset():
    """Resetting ``_benchmark_bars_cache`` (mirroring what
    ``_run_design_attempt`` does at the top of a new attempt) causes exactly
    one additional fetch, proving the memo doesn't leak stale benchmark data
    across attempts."""
    stub = StubMarketDataService()
    orch = orchestrator(stub)
    config = _config(walk_forward_enabled=True, n_folds=5, benchmark_composition="60_40")
    trades = trades_across_year(n_per_month=4)
    market_data = {"AAPL": stub_bars("AAPL")}

    orch._evaluate_walk_forward(spec(), market_data, config, trades, _base_metrics())
    orch._evaluate_walk_forward(spec(), market_data, config, trades, _base_metrics())
    calls_within_attempt = len(stub.calls)
    assert calls_within_attempt == 1

    orch._benchmark_bars_cache = {}  # simulates a new _run_design_attempt
    orch._evaluate_walk_forward(spec(), market_data, config, trades, _base_metrics())

    assert len(stub.calls) == calls_within_attempt + 1


def test_cached_fetch_benchmark_bars_keys_on_as_of():
    """The memo key includes ``as_of``, so a spec pinned to a particular
    historical snapshot still gets its own fetch rather than reusing bars
    fetched for a different snapshot."""
    stub = StubMarketDataService()
    orch = orchestrator(stub)

    orch._cached_fetch_benchmark_bars(
        symbols=["SPY", "AGG"],
        asset_class="stocks",
        start_date="2022-01-03",
        end_date="2022-12-30",
        as_of=None,
    )
    orch._cached_fetch_benchmark_bars(
        symbols=["SPY", "AGG"],
        asset_class="stocks",
        start_date="2022-01-03",
        end_date="2022-12-30",
        as_of=None,
    )
    assert len(stub.calls) == 1

    orch._cached_fetch_benchmark_bars(
        symbols=["SPY", "AGG"],
        asset_class="stocks",
        start_date="2022-01-03",
        end_date="2022-12-30",
        as_of="2022-06-01",
    )
    assert len(stub.calls) == 2
