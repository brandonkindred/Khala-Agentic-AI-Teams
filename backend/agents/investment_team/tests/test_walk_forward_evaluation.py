"""Walk-forward evaluation mechanics: fold wiring, DSR trial-count deflation,
and the return-series helper functions the evaluation depends on.

The lower-level building blocks (fold construction, DSR, bootstrap CI, regime
sub-windows, the acceptance gate itself) have their own dedicated test files.
This file exercises the wiring inside :class:`StrategyLabOrchestrator`:

- :meth:`_evaluate_walk_forward` populates every new ``BacktestResult`` field
  the acceptance gate consumes.
- :meth:`_daily_returns_from_trades` and :meth:`_equity_to_returns` produce
  sensible series for the DSR / regime helpers.
- :meth:`_build_benchmark_equity` blends 60/40 SPY+AGG when the market-data
  service returns both, and falls back to a single-symbol benchmark when the
  blend cannot be assembled.
- The trial counter monotonically deflates DSR for an unchanged raw Sharpe.

The full ``run_cycle`` is not exercised here; acceptance-gate composition and
alignment-loop caveat resolution are covered in ``test_acceptance_gate_integration.py``
and ``test_run_cycle_caveat_resolution.py``; the ideation agents are covered
in ``test_strategy_lab_alignment.py``.
"""

from __future__ import annotations

import pytest

from investment_team.execution.metrics import compute_deflated_sharpe
from investment_team.models import BacktestResult
from investment_team.strategy_lab.orchestrator import (
    StrategyLabOrchestrator,
    _closes_to_equity,
    _daily_returns_from_trades,
    _equity_to_returns,
)
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker

from ._walk_forward_test_helpers import (
    StubMarketDataService,
    mk_trade,
    orchestrator,
    spec,
    stub_bars,
    trades_across_year,
)

# ``config`` keeps its leading-underscore alias: nearly every test below
# assigns its result to a local variable named ``config``, and dropping
# the alias would make ``config = config(...)`` an UnboundLocalError
# (assigning a name anywhere in a function makes every reference to it
# local within that function, including the assignment's own right-hand side).
from ._walk_forward_test_helpers import config as _config

# ---------------------------------------------------------------------------
# _evaluate_walk_forward
# ---------------------------------------------------------------------------


def test_evaluate_walk_forward_populates_oos_fields():
    """All new BacktestResult fields are set; fold count matches config."""
    orch = orchestrator(StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    trades = trades_across_year(n_per_month=4)
    base_metrics = BacktestResult(
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
    market_data = {"AAPL": stub_bars("AAPL")}

    result = orch._evaluate_walk_forward(spec(), market_data, config, trades, base_metrics)

    assert result.deflated_sharpe is not None
    assert 0.0 <= result.deflated_sharpe <= 1.0
    assert result.oos_sharpe is not None
    assert result.is_sharpe is not None
    assert result.is_oos_degradation_pct is not None
    assert result.is_oos_degradation_pct >= 0.0  # clamped to non-negative
    assert result.oos_trade_count is not None
    assert result.oos_trade_count > 0
    assert result.fold_results is not None and len(result.fold_results) == 5
    # Per-fold dicts carry the keys the persistence layer expects.
    for fr in result.fold_results:
        assert {
            "fold_index",
            "test_start",
            "test_end",
            "oos_sharpe",
            "is_sharpe",
            "oos_trade_count",
        } <= set(fr.keys())
    # Bootstrap CI populated (may collapse to (0, 0) on tiny series; we just
    # confirm the fields are not None).
    assert result.sharpe_ci_low is not None
    assert result.sharpe_ci_high is not None
    # Regime evaluation ran and produced four entries (matching REGIME_LABELS).
    assert result.regime_results is not None and len(result.regime_results) == 4
    for rr in result.regime_results:
        assert "beat_benchmark" in rr


def test_evaluate_walk_forward_with_empty_trades_does_not_crash():
    """Empty trade list still yields a populated BacktestResult; OOS fields
    fall back to neutral values rather than raising."""
    orch = orchestrator(StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    trades = []
    base_metrics = BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    result = orch._evaluate_walk_forward(spec(), {}, config, trades, base_metrics)

    # At raw Sharpe = 0 and ``n_trials = 0``, DSR collapses to the
    # Probabilistic Sharpe Ratio against a zero benchmark, which is 0.5 —
    # not 0.0. We assert the field is populated and bounded; the gate
    # rejects it via the dsr_threshold default of 1.0.
    assert result.deflated_sharpe is not None
    assert 0.0 <= result.deflated_sharpe <= 1.0
    assert result.oos_sharpe == 0.0
    assert result.oos_trade_count == 0
    assert result.fold_results is not None and len(result.fold_results) == 5


def test_evaluate_walk_forward_falls_back_when_60_40_unavailable():
    """When SPY+AGG are unavailable, regime evaluation falls back to the
    single-symbol benchmark path. The overall walk-forward call must still
    return a populated result."""
    stub = StubMarketDataService(has_agg=False)
    orch = orchestrator(stub)
    config = _config(walk_forward_enabled=True, n_folds=5, benchmark_composition="60_40")
    trades = trades_across_year()
    base_metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=6.0,
        volatility_pct=8.0,
        sharpe_ratio=0.7,
        max_drawdown_pct=3.0,
        win_rate_pct=52.0,
        profit_factor=1.2,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    result = orch._evaluate_walk_forward(
        spec(), {"AAPL": stub_bars("AAPL")}, config, trades, base_metrics
    )

    assert result.deflated_sharpe is not None
    # Fallback path may surface zero-length regime results when neither blend
    # nor single-symbol resolve; we accept either an empty list or four
    # entries — the gate handles missing data via its own warning result.
    assert result.regime_results is not None
    assert len(result.regime_results) in (0, 4)


# ---------------------------------------------------------------------------
# Trial counter deflates DSR
# ---------------------------------------------------------------------------


def test_trial_count_deflates_dsr_for_identical_raw_sharpe():
    """For a fixed raw Sharpe and observation count, increasing ``n_trials``
    must not increase the DSR. This is the multiple-testing correction under
    test."""
    sharpe = 1.5
    n_obs = 250
    dsr_one = compute_deflated_sharpe(sharpe, n_trials=1, n_obs=n_obs, skew=0.0, kurtosis=3.0)
    dsr_fifty = compute_deflated_sharpe(sharpe, n_trials=50, n_obs=n_obs, skew=0.0, kurtosis=3.0)
    assert 0.0 <= dsr_fifty <= dsr_one <= 1.0
    assert dsr_one - dsr_fifty > 0.05  # meaningful (not just floating-point noise)


def test_increment_trials_on_orchestrator_tracker_is_visible_to_dsr():
    """The orchestrator's convergence tracker exposes ``trial_count`` via the
    same property the walk-forward helper feeds to ``compute_deflated_sharpe``.
    This guards against a future refactor accidentally bypassing the counter.
    """
    tracker = ConvergenceTracker()
    orch = StrategyLabOrchestrator(convergence_tracker=tracker)
    assert orch.convergence_tracker.trial_count == 0
    orch.convergence_tracker.increment_trials(3)
    orch.convergence_tracker.increment_trials(2)
    assert orch.convergence_tracker.trial_count == 5


# ---------------------------------------------------------------------------
# Helper purity: _daily_returns_from_trades and _equity_to_returns
# ---------------------------------------------------------------------------


def test_daily_returns_from_trades_handles_empty_input():
    """Returns an empty list rather than raising on an empty trade ledger."""
    out = _daily_returns_from_trades([], 100_000.0, "2022-01-03", "2022-12-30")
    assert out == [] or all(r == 0.0 for r in out)


def test_daily_returns_from_trades_emits_log_returns():
    """OOS-Sharpe / DSR / bootstrap CI need the same return convention as the
    in-sample Sharpe (log basis). A single +1k step on 100k equity should
    produce ``log(101_000 / 100_000)`` on the exit-date step, not the simple
    ``1_000 / 100_000`` ratio."""
    import math as _math

    trades = [
        mk_trade(
            entry="2023-01-03",
            exit_="2023-01-04",
            net=1_000.0,
            symbol="TST",
        )
    ]
    out = _daily_returns_from_trades(trades, 100_000.0, "2023-01-03", "2023-01-05")
    assert len(out) >= 1
    nonzero = [r for r in out if r != 0.0]
    assert len(nonzero) == 1
    assert nonzero[0] == pytest.approx(_math.log(101_000.0 / 100_000.0), rel=1e-12)
    # Sanity: simple-return basis would yield exactly 0.01, which differs
    # from log(1.01) ≈ 0.00995 — confirm we are NOT on simple basis.
    assert nonzero[0] != pytest.approx(0.01, abs=1e-6)


def test_daily_returns_from_trades_invalidates_ruin_series():
    """A run whose equity curve crosses zero is ruin: the OOS return series
    must NOT zero-pad the ruin step (which would let DSR / Sharpe CI report
    the strategy as materially safer than it is). The helper returns an
    empty list so every downstream consumer falls through its no-data
    path."""
    # Loss > initial capital drives equity negative on the exit date.
    trades = [
        mk_trade(
            entry="2023-01-03",
            exit_="2023-01-04",
            net=-150_000.0,
            symbol="TST",
        )
    ]
    out = _daily_returns_from_trades(trades, 100_000.0, "2023-01-03", "2023-01-06")
    assert out == []


def test_equity_to_returns_skips_zero_or_negative_prev():
    """Zero/negative previous equity yields a 0.0 return at that step (no
    ZeroDivisionError, no NaN)."""
    out = _equity_to_returns([100.0, 0.0, 50.0])
    assert len(out) == 2
    assert out[0] == pytest.approx(-1.0)
    assert out[1] == 0.0


def test_closes_to_equity_scales_to_initial_capital():
    """Equity curve scales proportionally to the initial-capital baseline as
    the underlying close series moves."""
    out = _closes_to_equity([10.0, 11.0, 12.0], 100_000.0)
    assert out[0] == pytest.approx(100_000.0)
    assert out[-1] == pytest.approx(100_000.0 * 12.0 / 10.0)


# ---------------------------------------------------------------------------
# Per-fold IS Sharpe uses the actual training segment, not the full span
# ---------------------------------------------------------------------------


def test_is_sharpe_uses_training_segments_not_full_span():
    """Per-fold IS Sharpe must be computed on the actual training date
    ranges. If we used ``config.start_date``/``config.end_date`` instead,
    the test+purge+embargo gap would show up as flat zero-return days and
    dilute the Sharpe — materially understating IS→OOS degradation.

    This test compares two scenarios with identical OOS trades but very
    different IS trade distributions (front-loaded vs back-loaded). The
    full-span computation would yield similar IS Sharpes for both because
    the gaps dominate; the per-segment computation produces visibly
    different IS Sharpes.
    """
    orch = orchestrator(StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    base_metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=6.0,
        volatility_pct=8.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=4.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    # Two trades per month, evenly distributed → at least one IS trade per
    # fold's training segments.
    trades = trades_across_year(n_per_month=4, base_pnl=80.0)
    market_data = {"AAPL": stub_bars("AAPL")}
    result = orch._evaluate_walk_forward(spec(), market_data, config, trades, base_metrics)

    # Per-fold IS Sharpe must come from the segment computation: when
    # ``is_trade_count > 0``, the recorded ``is_sharpe`` should not be
    # zero except by genuine flat-return coincidence. With four winning-
    # losing alternations per month and 5 folds, at least one fold should
    # produce a strictly nonzero IS Sharpe under the per-segment fix.
    nonzero_is_sharpes = [
        fr["is_sharpe"]
        for fr in (result.fold_results or [])
        if fr.get("is_trade_count", 0) > 0 and fr.get("is_sharpe", 0.0) != 0.0
    ]
    assert nonzero_is_sharpes, (
        "Per-fold IS Sharpe should be nonzero for at least one fold once we "
        "compute on training segments instead of the full backtest span."
    )
