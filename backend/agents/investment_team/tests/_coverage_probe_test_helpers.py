"""Shared fixtures + helpers for the coverage-probe test suite.

Not a test module — the leading underscore keeps pytest from collecting
it. Imported from the split test files (``test_coverage_probe_*.py``)
to avoid duplicating boilerplate fixtures across them.
"""

from __future__ import annotations

import textwrap
from typing import Any

import pandas as pd

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    StrategySpec,
    SubconditionCoverage,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult


def make_config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-06-30",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def make_spec(strategy_code: str | None) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-coverage-stage-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=25),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": 5},
        speculative=False,
        strategy_code=strategy_code,
    )


def make_flat_df(n: int, close: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + 1.0] * n,
            "low": [close - 1.0] * n,
            "close": [close] * n,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def make_diag(
    *,
    category: str | None = None,
    closed: int = 0,
    orders_accepted: int = 0,
) -> BacktestExecutionDiagnostics:
    return BacktestExecutionDiagnostics(
        zero_trade_category=category,  # type: ignore[arg-type]
        closed_trades=closed,
        orders_accepted=orders_accepted,
    )


def make_exec_result(diagnostics: BacktestExecutionDiagnostics | None) -> StrategyRunResult:
    return StrategyRunResult(
        success=True,
        trades=[],
        execution_diagnostics=diagnostics,
    )


def never_called_run_strategy_code(*args: Any, **kwargs: Any) -> StrategyRunResult:
    raise AssertionError("run_strategy_code must not be invoked")


def make_report(
    category: CoverageCategory,
    *,
    subconditions: list[SubconditionCoverage] | None = None,
    blockers: list[LikelyBlocker] | None = None,
    warmup: int = 0,
    bars: int = 0,
    symbols: int = 0,
) -> CoverageReport:
    return CoverageReport(
        coverage_category=category,
        subconditions=subconditions or [],
        likely_blockers=blockers or [],
        warmup_bars_required=warmup,
        bars_checked=bars,
        symbols_checked=symbols,
    )


def make_ohlcv_bar(date: str, close: float) -> OHLCVBar:
    return OHLCVBar(date=date, open=close, high=close + 1, low=close - 1, close=close, volume=1e6)


def make_ohlcv_series(n: int = 120, close: float = 100.0) -> list[OHLCVBar]:
    dates = pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    return [make_ohlcv_bar(d, close) for d in dates]


def unknown_indicator(*args: Any, **kwargs: Any) -> CoverageReport:
    """Stub indicator probe that always reports UNKNOWN_LOW_COVERAGE.

    Forces ``run_coverage_stage`` into the runtime-reexecution branch
    when monkey-patched in as the indicator probe.
    """
    return make_report(CoverageCategory.UNKNOWN_LOW_COVERAGE)


def runtime_capable_code() -> str:
    """Strategy source the runtime instrumenter can find an ``on_bar``
    predicate in (so ``rule_index.rules`` is non-empty)."""
    return textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if self.custom_helper(bar):
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )
