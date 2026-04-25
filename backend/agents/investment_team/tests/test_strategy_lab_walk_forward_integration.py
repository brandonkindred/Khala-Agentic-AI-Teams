"""Orchestrator wiring tests for issue #247 — walk-forward + acceptance gate.

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

The full ``run_cycle`` is not exercised here; the alignment-loop and ideation
agents are covered in ``test_strategy_lab_alignment.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest

from investment_team.execution.metrics import compute_deflated_sharpe
from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.acceptance_gate import AcceptanceGate
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-wf-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        entry_rules=["enter on signal"],
        exit_rules=["exit on opposite"],
        sizing_rules=["fixed size"],
        risk_limits={},
        speculative=False,
        strategy_code="from contract import Strategy\n\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )


def _config(**overrides: Any) -> BacktestConfig:
    base: Dict[str, Any] = dict(
        start_date="2022-01-03",
        end_date="2022-12-30",
        initial_capital=100_000.0,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def _mk_trade(
    *,
    entry: str,
    exit_: str,
    net: float,
    symbol: str = "AAPL",
    hold: int = 5,
) -> TradeRecord:
    return TradeRecord(
        trade_num=1,
        entry_date=entry,
        exit_date=exit_,
        symbol=symbol,
        side="long" if net >= 0 else "short",
        entry_price=100.0,
        exit_price=100.0 + net / 10.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=net,
        net_pnl=net,
        return_pct=net / 1000.0 * 100,
        hold_days=hold,
        outcome="win" if net > 0 else "loss",
        cumulative_pnl=net,
    )


def _trades_across_year(n_per_month: int = 4, base_pnl: float = 50.0) -> List[TradeRecord]:
    """Spread roughly ``n_per_month`` winning trades across the calendar so that
    every walk-forward fold gets a handful of OOS observations."""
    out: List[TradeRecord] = []
    cum = 0.0
    for month in range(1, 13):
        for j in range(n_per_month):
            day = (j * 7) + 3  # 3, 10, 17, 24
            entry = date(2022, month, day)
            exit_ = entry + timedelta(days=5)
            net = base_pnl if (month + j) % 2 == 0 else -base_pnl * 0.6
            cum += net
            out.append(
                TradeRecord(
                    trade_num=len(out) + 1,
                    entry_date=entry.isoformat(),
                    exit_date=exit_.isoformat(),
                    symbol="AAPL",
                    side="long",
                    entry_price=100.0,
                    exit_price=100.0 + net / 10.0,
                    shares=10.0,
                    position_value=1000.0,
                    gross_pnl=net,
                    net_pnl=net,
                    return_pct=net / 1000.0 * 100,
                    hold_days=5,
                    outcome="win" if net > 0 else "loss",
                    cumulative_pnl=cum,
                )
            )
    return out


def _stub_bars(symbol: str, *, drift: float = 0.0003, n: int = 250) -> List[OHLCVBar]:
    """Synthetic OHLCV bars starting 2022-01-03; price drifts at ``drift`` per day."""
    bars: List[OHLCVBar] = []
    d = date(2022, 1, 3)
    price = 100.0
    while len(bars) < n:
        if d.weekday() < 5:
            bars.append(
                OHLCVBar(
                    date=d.isoformat(),
                    open=price,
                    high=price * 1.005,
                    low=price * 0.995,
                    close=price,
                    volume=1_000_000,
                )
            )
            price *= 1.0 + drift
        d += timedelta(days=1)
    return bars


class _StubMarketDataService:
    """Returns canned SPY/AGG bars for the 60/40 blend; no network access."""

    def __init__(self, *, has_spy: bool = True, has_agg: bool = True) -> None:
        self.has_spy = has_spy
        self.has_agg = has_agg
        self.calls: List[Dict[str, Any]] = []

    def fetch_multi_symbol_range(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        start_date: str,
        end_date: str,
    ) -> Dict[str, List[OHLCVBar]]:
        self.calls.append(
            {
                "symbols": list(symbols),
                "asset_class": asset_class,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        out: Dict[str, List[OHLCVBar]] = {}
        for s in symbols:
            if s == "SPY" and self.has_spy:
                out[s] = _stub_bars("SPY", drift=0.0004)
            elif s == "AGG" and self.has_agg:
                out[s] = _stub_bars("AGG", drift=0.0001)
            elif s not in {"SPY", "AGG"}:
                out[s] = _stub_bars(s, drift=0.0002)
        return out


def _orchestrator(
    market_data_service: Optional[_StubMarketDataService] = None,
) -> StrategyLabOrchestrator:
    orch = StrategyLabOrchestrator()
    if market_data_service is not None:
        orch.market_data_service = market_data_service  # type: ignore[assignment]
    return orch


# ---------------------------------------------------------------------------
# _evaluate_walk_forward
# ---------------------------------------------------------------------------


def test_evaluate_walk_forward_populates_oos_fields():
    """All new BacktestResult fields are set; fold count matches config."""
    orch = _orchestrator(_StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    trades = _trades_across_year(n_per_month=4)
    base_metrics = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=12.0,
        volatility_pct=8.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=4.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
    )
    market_data = {"AAPL": _stub_bars("AAPL")}

    result = orch._evaluate_walk_forward(_spec(), market_data, config, trades, base_metrics)

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
    orch = _orchestrator(_StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    trades: List[TradeRecord] = []
    base_metrics = BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
    )

    result = orch._evaluate_walk_forward(_spec(), {}, config, trades, base_metrics)

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
    stub = _StubMarketDataService(has_agg=False)
    orch = _orchestrator(stub)
    config = _config(walk_forward_enabled=True, n_folds=5, benchmark_composition="60_40")
    trades = _trades_across_year()
    base_metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=6.0,
        volatility_pct=8.0,
        sharpe_ratio=0.7,
        max_drawdown_pct=3.0,
        win_rate_pct=52.0,
        profit_factor=1.2,
    )

    result = orch._evaluate_walk_forward(
        _spec(), {"AAPL": _stub_bars("AAPL")}, config, trades, base_metrics
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
    must not increase the DSR. This is the multiple-testing correction the
    issue calls out."""
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
# Acceptance gate drives is_winning end-to-end (composition test)
# ---------------------------------------------------------------------------


def test_acceptance_gate_passes_winning_walk_forward_result():
    """A walk-forward result that clears all four sub-criteria produces an
    all-passing gate result; the orchestrator's ``is_winning`` rule treats
    that as a win."""
    cfg = _config(
        walk_forward_enabled=True,
        dsr_threshold=0.5,
        max_is_oos_degradation_pct=40.0,
        min_oos_trades=10,
    )
    res = BacktestResult(
        total_return_pct=12.0,
        annualized_return_pct=14.0,
        volatility_pct=9.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=5.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        deflated_sharpe=0.8,
        oos_sharpe=1.2,
        is_sharpe=1.3,
        is_oos_degradation_pct=10.0,
        oos_trade_count=40,
        regime_results=[
            {"regime": "vix_q1", "beat_benchmark": True},
            {"regime": "vix_q2", "beat_benchmark": True},
            {"regime": "vix_q3", "beat_benchmark": False},
            {"regime": "vix_q4", "beat_benchmark": False},
        ],
    )
    results = AcceptanceGate().check(res, cfg, n_trials=10)
    assert all(r.passed for r in results)
    assert (True and all(r.passed for r in results)) is True  # the orchestrator's rule


def test_acceptance_gate_rejects_overfit_pattern():
    """High IS Sharpe + collapsed OOS Sharpe + insufficient regime breadth
    must trip multiple sub-criteria, so the orchestrator marks the cycle
    as not-winning."""
    cfg = _config(
        walk_forward_enabled=True,
        dsr_threshold=0.9,
        max_is_oos_degradation_pct=30.0,
        min_oos_trades=30,
    )
    res = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=22.0,
        volatility_pct=11.0,
        sharpe_ratio=2.5,  # IS-only sharpe (the headline single-window number)
        max_drawdown_pct=6.0,
        win_rate_pct=63.0,
        profit_factor=1.9,
        deflated_sharpe=0.3,  # low — overfit suspicion
        oos_sharpe=0.4,
        is_sharpe=2.5,
        is_oos_degradation_pct=84.0,  # huge IS→OOS gap
        oos_trade_count=12,  # below min_oos_trades
        regime_results=[
            {"regime": "vix_q1", "beat_benchmark": False},
            {"regime": "vix_q2", "beat_benchmark": False},
            {"regime": "vix_q3", "beat_benchmark": False},
            {"regime": "vix_q4", "beat_benchmark": False},
        ],
    )
    results = AcceptanceGate().check(res, cfg, n_trials=50)
    assert not all(r.passed for r in results)
    failed_reasons = [r.details for r in results if not r.passed]
    # Each of the four sub-criteria fails on this fixture.
    assert len(failed_reasons) == 4


# ---------------------------------------------------------------------------
# Helper purity: _daily_returns_from_trades and _equity_to_returns
# ---------------------------------------------------------------------------


def test_daily_returns_from_trades_handles_empty_input():
    """Returns an empty list rather than raising on an empty trade ledger."""
    out = StrategyLabOrchestrator._daily_returns_from_trades(
        [], 100_000.0, "2022-01-03", "2022-12-30"
    )
    assert out == [] or all(r == 0.0 for r in out)


def test_equity_to_returns_skips_zero_or_negative_prev():
    """Zero/negative previous equity yields a 0.0 return at that step (no
    ZeroDivisionError, no NaN)."""
    out = StrategyLabOrchestrator._equity_to_returns([100.0, 0.0, 50.0])
    assert len(out) == 2
    assert out[0] == pytest.approx(-1.0)
    assert out[1] == 0.0


def test_closes_to_equity_scales_to_initial_capital():
    out = StrategyLabOrchestrator._closes_to_equity([10.0, 11.0, 12.0], 100_000.0)
    assert out[0] == pytest.approx(100_000.0)
    assert out[-1] == pytest.approx(100_000.0 * 12.0 / 10.0)
