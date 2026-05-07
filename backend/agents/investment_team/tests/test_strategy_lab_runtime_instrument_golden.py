"""Round-trip behaviour-preservation tests for the runtime instrumenter (#449).

For each known fixture strategy, run the original and instrumented sources
through the in-process backtest harness and assert that the resulting trade
list is byte-identical. This is the acceptance criterion from the issue:
"round-trip rewrite preserves the observed trade list on a known fixture
strategy".
"""

from __future__ import annotations

from typing import List

import pytest

from investment_team.models import BacktestConfig, StrategySpec, TradeRecord
from investment_team.strategy_lab.coverage_probe import instrument_strategy_code
from investment_team.trading_service.modes.backtest import run_backtest

from .golden.fixtures import DEFAULT_DAYS, golden_market_data
from .golden.strategies import (
    BUY_AND_HOLD_CODE,
    OVERFIT_HARDCODED_DATES_CODE,
    ROUND_TRIP_CODE,
    SMA_CROSSOVER_CODE,
)


def _make_spec(name: str, code: str) -> StrategySpec:
    return StrategySpec(
        strategy_id=f"runtime-instrument-{name}",
        authored_by="runtime-instrument-tests",
        asset_class="stocks",
        hypothesis="instrumented round-trip",
        signal_definition="see strategies.py",
        strategy_code=code,
    )


def _make_config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_model="optimistic",
    )


def _trade_tuples(trades: List[TradeRecord]) -> list[tuple]:
    return [
        (
            t.trade_num,
            t.symbol,
            t.side,
            t.entry_date,
            t.exit_date,
            round(t.shares, 4),
            round(t.gross_pnl, 2),
            round(t.net_pnl, 2),
            t.hold_days,
            t.outcome,
        )
        for t in trades
    ]


def _run_pair(name: str, code: str) -> tuple[list[tuple], list[tuple]]:
    rewritten, index = instrument_strategy_code(code)
    assert rewritten != code, "fixture should produce at least one wrapped subcondition"
    assert len(index.rules) > 0

    config = _make_config()
    market_data = golden_market_data(n_days=DEFAULT_DAYS)

    original_result = run_backtest(
        strategy=_make_spec(f"{name}-original", code),
        config=config,
        market_data=market_data,
    )
    instrumented_result = run_backtest(
        strategy=_make_spec(f"{name}-instrumented", rewritten),
        config=config,
        market_data=market_data,
    )

    return _trade_tuples(original_result.trades), _trade_tuples(instrumented_result.trades)


@pytest.mark.slow_subprocess
def test_sma_crossover_trades_unchanged() -> None:
    original, instrumented = _run_pair("sma_crossover", SMA_CROSSOVER_CODE)
    assert original == instrumented


@pytest.mark.slow_subprocess
def test_round_trip_trades_unchanged() -> None:
    original, instrumented = _run_pair("round_trip", ROUND_TRIP_CODE)
    assert original == instrumented


@pytest.mark.slow_subprocess
def test_buy_and_hold_trades_unchanged() -> None:
    original, instrumented = _run_pair("buy_and_hold", BUY_AND_HOLD_CODE)
    assert original == instrumented


@pytest.mark.slow_subprocess
def test_overfit_hardcoded_dates_trades_unchanged() -> None:
    original, instrumented = _run_pair("overfit_hardcoded_dates", OVERFIT_HARDCODED_DATES_CODE)
    assert original == instrumented
