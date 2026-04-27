"""End-to-end tests for the new streaming Trading Service.

Covers:
* A minimal SMA-crossover strategy produces at least one round-trip trade
  against deterministic synthetic bars.
* A strategy that tries to read future data from a non-existent attribute
  aborts the run with ``lookahead_violation`` rather than silently skipping.
* ``modes.backtest.run_backtest`` raises ``ValueError`` when the strategy
  has no ``strategy_code`` (the LLM-per-bar fallback is intentionally gone).
"""

from __future__ import annotations

import textwrap
from typing import Dict, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, StrategySpec
from investment_team.trading_service.modes.backtest import run_backtest


def _uptrend_then_down_bars(symbol_bars: Dict[str, List[OHLCVBar]]) -> None:
    """Populate ``symbol_bars`` with a clean up-then-down pattern.

    The shape is deterministic so a simple SMA(5) crossover produces exactly
    one long round-trip trade: uptrend (bars 0-14) triggers the entry, the
    subsequent downturn (bars 15-29) triggers the exit.
    """
    bars: List[OHLCVBar] = []
    # 30 calendar days starting 2024-01-01 (spans a month boundary, fine).
    base = 100.0
    for i in range(15):
        price = base + i * 2.0  # steady +2 per bar
        bars.append(_mkbar(i + 1, price))
    for i in range(15):
        price = (base + 28.0) - (i + 1) * 2.5  # accelerating decline
        bars.append(_mkbar(16 + i, price))
    symbol_bars["AAA"] = bars


def _mkbar(day_of_month: int, close: float) -> OHLCVBar:
    month = 1 if day_of_month <= 31 else 2
    day = day_of_month if month == 1 else day_of_month - 31
    return OHLCVBar(
        date=f"2024-{month:02d}-{day:02d}",
        open=close - 0.2,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1_000_000,
    )


_SMA_STRATEGY_CODE = textwrap.dedent('''\
    """Tiny SMA(5) crossover — deterministic, no randomness, no LLM.

    Enters long when the current close crosses above SMA(5) and no position
    is open; exits when the current close crosses below SMA(5).
    """
    from contract import OrderSide, OrderType, Strategy


    class SmaCrossover(Strategy):
        WINDOW = 5

        def on_bar(self, ctx, bar):
            history = ctx.history(bar.symbol, self.WINDOW)
            if len(history) < self.WINDOW:
                return
            sma = sum(b.close for b in history) / self.WINDOW
            pos = ctx.position(bar.symbol)
            if pos is None and bar.close > sma:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=10,
                    order_type=OrderType.MARKET,
                    reason="sma_cross_up",
                )
            elif pos is not None and bar.close < sma:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,  # opposite side closes the long
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="sma_cross_down",
                )
''')


_LOOKAHEAD_STRATEGY_CODE = textwrap.dedent('''\
    """Red-team strategy that tries to peek at future data."""
    from contract import Strategy


    class Peeker(Strategy):
        def on_bar(self, ctx, bar):
            # Attempting to access a non-existent "future" attribute must
            # surface as a classified lookahead_violation — not be silently
            # ignored. ``Bar`` has no such field.
            _ = bar.next_close  # noqa: F841 — intentional AttributeError
''')


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-02-15",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        metrics_engine="legacy",
    )


def test_trading_service_runs_sma_strategy_and_produces_trade() -> None:
    """Event-driven Strategy subclass → at least one round-trip trade."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-sma-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=["close > sma(5)"],
        exit_rules=["close < sma(5)"],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(
        strategy=strategy,
        config=_config(),
        market_data=market_data,
    )

    assert run.service_result.error is None, run.service_result.error
    assert not run.service_result.lookahead_violation
    assert len(run.trades) >= 1
    trade = run.trades[0]
    assert trade.symbol == "AAA"
    assert trade.side == "long"
    # Entry occurred after the SMA warmup window.
    assert trade.entry_date >= "2024-01-06"
    # Exit happened during the downtrend phase (bars after day 15).
    assert trade.exit_date > trade.entry_date


def test_trading_service_surfaces_lookahead_violation() -> None:
    """A strategy touching a non-existent forward field aborts the run cleanly."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-peeker-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="peek at future bars (should fail)",
        signal_definition="future bar access",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_LOOKAHEAD_STRATEGY_CODE,
    )

    run = run_backtest(
        strategy=strategy,
        config=_config(),
        market_data=market_data,
    )

    assert run.service_result.error is not None
    assert run.service_result.lookahead_violation is True
    assert not run.trades


def test_run_backtest_without_strategy_code_raises() -> None:
    """The LLM-per-bar fallback is removed; no strategy_code must fail fast."""
    strategy = StrategySpec(
        strategy_id="strat-no-code",
        authored_by="legacy",
        asset_class="equity",
        hypothesis="h",
        signal_definition="s",
        strategy_code=None,
    )
    with pytest.raises(ValueError, match="strategy_code is required"):
        run_backtest(strategy=strategy, config=_config(), market_data={})


# ---------------------------------------------------------------------------
# Issue #375 — preflight data integrity gate
# ---------------------------------------------------------------------------


def test_run_backtest_attaches_data_quality_report() -> None:
    """Happy path: clean market data → report present, severity == 'ok'."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-sma-dq-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=["close > sma(5)"],
        exit_rules=["close < sma(5)"],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    assert run.result.data_quality_report is not None
    assert run.result.data_quality_report["severity"] == "ok"
    assert "AAA" in run.result.data_quality_report["per_symbol"]


def test_run_backtest_strict_fails_on_ohlc_violation() -> None:
    """A bar with high < open trips the gate before TradingService runs."""
    from investment_team.execution.data_quality import DataIntegrityError

    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)
    bars = market_data["AAA"]
    # Corrupt one bar so high < max(open, close).
    bars[10] = bars[10].model_copy(update={"high": bars[10].open - 5.0})

    strategy = StrategySpec(
        strategy_id="strat-dq-fail",
        authored_by="tests",
        asset_class="equity",
        hypothesis="h",
        signal_definition="s",
        strategy_code=_SMA_STRATEGY_CODE,
    )

    with pytest.raises(DataIntegrityError) as excinfo:
        run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    assert excinfo.value.report.severity == "fail"
    assert excinfo.value.report.per_symbol["AAA"].ohlc_violations == 1
