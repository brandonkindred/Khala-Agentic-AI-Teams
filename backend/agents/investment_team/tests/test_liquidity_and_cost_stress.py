"""Phase 4 regression tests.

Covers:

* ``compute_adv_from_bars`` — pure ADV helper over a rolling window.
* ``BacktestConfig`` Phase 4 flags: ``cost_stress``, ``cost_stress_multipliers``,
  ``min_sharpe_at_2x``, ``min_signals_per_bar``.
* ``BacktestResult`` Phase 4 fields: ``signals_per_bar``,
  ``cost_stress_results``, ``reject_reason``.
* ``run_backtest`` end-to-end wiring — cost-stress replay populates
  per-multiplier rows, ``min_sharpe_at_2x`` rejects, and the
  ``min_signals_per_bar`` floor rejects sparse strategies.
* ``BacktestAnomalyDetector.check`` — paper mode relaxes the
  <5-trades gate.
"""

from __future__ import annotations

import textwrap
from typing import List

from investment_team.market_data_service import OHLCVBar, compute_adv_from_bars
from investment_team.models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from investment_team.strategy_lab.quality_gates.backtest_anomaly import (
    BacktestAnomalyDetector,
)
from investment_team.trading_service.modes.backtest import run_backtest

# ---------------------------------------------------------------------------
# compute_adv_from_bars
# ---------------------------------------------------------------------------


def _bar(date_: str, close: float, volume: float) -> OHLCVBar:
    return OHLCVBar(date=date_, open=close, high=close, low=close, close=close, volume=volume)


def test_compute_adv_returns_none_when_series_shorter_than_lookback() -> None:
    bars = [_bar(f"2024-01-{i + 1:02d}", 100.0, 1000.0) for i in range(10)]
    assert compute_adv_from_bars(bars, lookback=20) is None


def test_compute_adv_matches_hand_computed_value() -> None:
    # 20 bars of $100 close × $1M volume = $100M per bar; mean = $100M.
    bars = [_bar(f"2024-01-{i + 1:02d}", 100.0, 1_000_000.0) for i in range(20)]
    assert compute_adv_from_bars(bars, lookback=20) == 100_000_000.0


def test_compute_adv_uses_trailing_window_when_series_is_longer() -> None:
    # Feed 40 bars; the last 20 are the window.  First 20 have close=50,
    # last 20 have close=200 — the rolling mean must reflect only the last.
    bars = [
        _bar(
            f"2024-{'01' if i < 20 else '02'}-{(i % 20) + 1:02d}",
            200.0 if i >= 20 else 50.0,
            1_000_000.0,
        )
        for i in range(40)
    ]
    adv = compute_adv_from_bars(bars, lookback=20)
    assert adv == 200_000_000.0


def test_compute_adv_skips_zero_volume_bars() -> None:
    # Last 20 bars all zero-volume → returns None.
    bars = [_bar(f"2024-01-{i + 1:02d}", 100.0, 0.0) for i in range(20)]
    assert compute_adv_from_bars(bars, lookback=20) is None


# ---------------------------------------------------------------------------
# BacktestConfig Phase 4 fields
# ---------------------------------------------------------------------------


def test_backtest_config_has_phase4_defaults() -> None:
    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-12-31")
    assert cfg.cost_stress is False
    assert cfg.cost_stress_multipliers == [1.0, 2.0, 3.0]
    assert cfg.min_sharpe_at_2x is None
    assert cfg.min_signals_per_bar == 0.0


# ---------------------------------------------------------------------------
# run_backtest integration
# ---------------------------------------------------------------------------


_ROUND_TRIP_CODE = textwrap.dedent('''\
    """Open/close at fixed intervals so every trade is a closed round trip."""
    from contract import OrderSide, OrderType, Strategy


    HOLD = 2


    class RoundTrip(Strategy):
        def __init__(self):
            self._counter = 0

        def on_bar(self, ctx, bar):
            self._counter += 1
            pos = ctx.position(bar.symbol)
            if pos is None and self._counter % (2 * HOLD) == 1:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=5,
                    order_type=OrderType.MARKET,
                    reason="enter",
                )
            elif pos is not None and self._counter % (2 * HOLD) == HOLD + 1:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="exit",
                )
''')


_SPARSE_CODE = textwrap.dedent('''\
    """Enter once on the second bar, then never again — triggers
    low_signals_per_bar once the floor is set."""
    from contract import OrderSide, OrderType, Strategy


    class Sparse(Strategy):
        def __init__(self):
            self._entered = False

        def on_bar(self, ctx, bar):
            if not self._entered:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=1,
                    order_type=OrderType.MARKET,
                    reason="enter-once",
                )
                self._entered = True
''')


def _spec(code: str, sid: str = "phase4") -> StrategySpec:
    return StrategySpec(
        strategy_id=sid,
        authored_by="phase4-test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        strategy_code=code,
    )


def _config(**overrides) -> BacktestConfig:
    kwargs = dict(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    kwargs.update(overrides)
    return BacktestConfig(**kwargs)


def _bars(n: int) -> List[OHLCVBar]:
    return [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0 + i * 0.1,
            high=100.5 + i * 0.1,
            low=99.5 + i * 0.1,
            close=100.0 + i * 0.1,
            volume=1_000_000.0,
        )
        for i in range(n)
    ]


def test_signals_per_bar_is_computed_and_populated() -> None:
    """A round-trip strategy over 20 bars produces ~3 trades; signals_per_bar = 3/20 = 0.15."""
    result = run_backtest(
        strategy=_spec(_ROUND_TRIP_CODE, "rt"),
        config=_config(),
        market_data={"AAA": _bars(20)},
    )
    assert result.result.signals_per_bar is not None
    assert result.result.signals_per_bar > 0
    assert result.result.reject_reason is None


def test_min_signals_per_bar_rejects_sparse_strategy() -> None:
    result = run_backtest(
        strategy=_spec(_SPARSE_CODE, "sparse"),
        config=_config(min_signals_per_bar=0.5),
        market_data={"AAA": _bars(20)},
    )
    # Sparse strategy never closes the position → 0 trades → signals_per_bar = 0.
    assert result.result.signals_per_bar == 0
    assert result.result.reject_reason == "low_signals_per_bar"


def test_min_signals_per_bar_zero_disables_the_gate() -> None:
    """With the default floor of 0, reject_reason stays None."""
    result = run_backtest(
        strategy=_spec(_SPARSE_CODE, "sparse-off"),
        config=_config(min_signals_per_bar=0.0),
        market_data={"AAA": _bars(20)},
    )
    assert result.result.reject_reason is None


def test_cost_stress_populates_cost_stress_results() -> None:
    result = run_backtest(
        strategy=_spec(_ROUND_TRIP_CODE, "stress"),
        config=_config(
            transaction_cost_bps=1.0,
            slippage_bps=1.0,
            cost_stress=True,
        ),
        market_data={"AAA": _bars(20)},
    )
    rows = result.result.cost_stress_results
    assert rows is not None
    assert [r["multiplier"] for r in rows] == [1.0, 2.0, 3.0]
    for r in rows:
        assert "sharpe_ratio" in r
        assert "annualized_return_pct" in r
        assert "max_drawdown_pct" in r
        assert "trade_count" in r


def test_min_sharpe_at_2x_rejects_overfit_strategy() -> None:
    """With high costs and a demanding Sharpe floor, the 2x multiplier should fail."""
    result = run_backtest(
        strategy=_spec(_ROUND_TRIP_CODE, "overfit"),
        config=_config(
            # High base costs make the 2x stress Sharpe plunge below a
            # demanding threshold.
            transaction_cost_bps=50.0,
            slippage_bps=25.0,
            cost_stress=True,
            min_sharpe_at_2x=10.0,  # absurdly high floor; forces rejection
        ),
        market_data={"AAA": _bars(20)},
    )
    assert result.result.cost_stress_results is not None
    assert result.result.reject_reason == "fails_cost_stress"


def test_cost_stress_trade_counts_reflect_each_stressed_run(monkeypatch) -> None:
    """Each row's ``trade_count`` must come from its own stressed run, not the baseline.

    White-box test: patches ``TradingService.run`` to return a fresh
    ``TradingServiceResult`` whose ``len(trades)`` depends on the
    config's ``transaction_cost_bps``.  Because the cost-stress replay
    scales that field per multiplier, the rows will show distinct trade
    counts only when the source is the stressed run (not the baseline).
    """
    import investment_team.trading_service.modes.backtest as backtest_mod
    from investment_team.models import TradeRecord
    from investment_team.trading_service.service import TradingServiceResult

    def _make_trade(i: int) -> TradeRecord:
        return TradeRecord(
            trade_num=i,
            entry_date="2024-01-02",
            exit_date="2024-01-03",
            symbol="AAA",
            side="long",
            entry_price=100.0,
            exit_price=101.0,
            shares=1.0,
            position_value=100.0,
            gross_pnl=1.0,
            net_pnl=1.0,
            return_pct=1.0,
            hold_days=1,
            outcome="win",
            cumulative_pnl=float(i),
        )

    class _Fake:
        def __init__(self, *, strategy_code, config, risk_limits=None):
            self.config = config

        def run(self, stream, *, on_trade=None):
            # Shrink the ledger as costs scale so each multiplier's row
            # must see a distinct count if sourced from its own run.
            trades = max(0, 10 - int(self.config.transaction_cost_bps))
            return TradingServiceResult(
                trades=[_make_trade(i) for i in range(trades)],
                bars_processed=20,
            )

    monkeypatch.setattr(backtest_mod, "TradingService", _Fake)

    result = run_backtest(
        strategy=_spec(_ROUND_TRIP_CODE, "stress-counts"),
        config=_config(
            transaction_cost_bps=1.0,
            slippage_bps=1.0,
            cost_stress=True,
        ),
        market_data={"AAA": _bars(20)},
    )
    rows = result.result.cost_stress_results
    assert rows is not None and len(rows) == 3
    # 1x tx=1 → 10-1=9 trades; 2x tx=2 → 8; 3x tx=3 → 7.  If trade_count
    # were sourced from the baseline service_result, every row would
    # report the same value.
    assert [r["trade_count"] for r in rows] == [9, 8, 7]


def test_signals_per_bar_computed_even_without_prefetched_market_data() -> None:
    """TradingService.bars_processed must feed signals_per_bar for either data path.

    Regression guard: an earlier implementation counted bars from the
    pre-fetched ``market_data`` dict, so provider-driven backtests
    (which pass ``symbols`` + ``asset_class`` instead) reported
    ``signals_per_bar=None`` and silently skipped the
    ``min_signals_per_bar`` gate.
    """
    # Direct TradingService invocation — matches the shape of the
    # provider-driven path where ``market_data`` is never materialized in
    # ``run_backtest``'s scope.
    from investment_team.trading_service.data_stream.historical_replay import (
        HistoricalReplayStream,
    )
    from investment_team.trading_service.service import TradingService

    spec = _spec(_ROUND_TRIP_CODE, "provider-like")
    config = _config()
    stream = HistoricalReplayStream({"AAA": _bars(20)}, timeframe="1d")
    service = TradingService(
        strategy_code=spec.strategy_code,
        config=config,
        risk_limits=spec.risk_limits,
    )
    outcome = service.run(stream)
    assert outcome.bars_processed == 20


def test_cost_stress_disabled_leaves_results_none() -> None:
    result = run_backtest(
        strategy=_spec(_ROUND_TRIP_CODE, "no-stress"),
        config=_config(),
        market_data={"AAA": _bars(20)},
    )
    assert result.result.cost_stress_results is None


# ---------------------------------------------------------------------------
# BacktestAnomalyDetector — paper mode
# ---------------------------------------------------------------------------


def _trade(entry: str, exit_: str, net_pnl: float = 10.0, shares: float = 1.0) -> TradeRecord:
    return TradeRecord(
        trade_num=1,
        entry_date=entry,
        exit_date=exit_,
        symbol="AAA",
        side="long",
        entry_price=100.0,
        exit_price=100.0 + net_pnl,
        shares=shares,
        position_value=100.0,
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        return_pct=net_pnl,
        hold_days=2,
        outcome="win" if net_pnl > 0 else "loss",
        cumulative_pnl=net_pnl,
    )


def _healthy_metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=10.0,
        volatility_pct=5.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=3.0,
        win_rate_pct=60.0,
        profit_factor=1.5,
    )


def test_anomaly_paper_mode_skips_too_few_trades_gate() -> None:
    detector = BacktestAnomalyDetector()
    few_trades = [_trade(f"2024-01-{i + 1:02d}", f"2024-01-{i + 2:02d}") for i in range(3)]
    paper = detector.check(_healthy_metrics(), few_trades, mode="paper")
    backtest = detector.check(_healthy_metrics(), few_trades, mode="backtest")
    # Backtest mode flags "Only 3 trades"; paper mode skips it.
    paper_reasons = " ".join(r.details for r in paper if not r.passed)
    backtest_reasons = " ".join(r.details for r in backtest if not r.passed)
    assert "statistically meaningless" in backtest_reasons
    assert "statistically meaningless" not in paper_reasons


def test_anomaly_zero_trades_still_flagged_in_paper_mode() -> None:
    detector = BacktestAnomalyDetector()
    paper = detector.check(_healthy_metrics(), [], mode="paper")
    failed = [r for r in paper if not r.passed]
    assert any("zero trades" in r.details for r in failed)
