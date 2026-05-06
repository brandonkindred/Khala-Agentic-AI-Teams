"""Unit tests for :func:`run_strategy_code` — the PR 3 compat shim.

Focus: mid-run strategy crashes that accumulate partial fills before
raising must surface as ``success=False`` with ``error_type="runtime_error"``,
not silently pass through as a truncated successful run. See the Codex
review on PR #185 for the original bug report.
"""

from __future__ import annotations

from typing import List

from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from investment_team.trading_service.modes import sandbox_compat
from investment_team.trading_service.modes.backtest import BacktestRunResult
from investment_team.trading_service.service import TradingServiceResult


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )


def _strategy() -> StrategySpec:
    return StrategySpec(
        strategy_id="s",
        authored_by="test",
        asset_class="equity",
        hypothesis="h",
        signal_definition="s",
        entry_rules=[],
        exit_rules=[],
        strategy_code="# unused — run_backtest is patched in tests below\n",
    )


def _partial_trade() -> TradeRecord:
    return TradeRecord(
        trade_num=1,
        entry_date="2024-01-02",
        exit_date="2024-01-05",
        symbol="AAA",
        side="long",
        entry_price=100.0,
        exit_price=102.0,
        shares=10,
        position_value=1_000.0,
        gross_pnl=20.0,
        net_pnl=20.0,
        return_pct=2.0,
        hold_days=3,
        outcome="win",
        cumulative_pnl=20.0,
    )


def _empty_metrics() -> BacktestResult:
    """Build a zero-valued BacktestResult for test stubs."""
    return BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
    )


def _patch_run_backtest(monkeypatch, *, service_result: TradingServiceResult) -> None:
    """Replace ``run_backtest`` with a stub returning a canned service result."""

    def _fake(*, strategy, config, market_data=None, **_kwargs):
        return BacktestRunResult(
            result=_empty_metrics(),
            trades=service_result.trades,
            service_result=service_result,
        )

    monkeypatch.setattr(sandbox_compat, "run_backtest", _fake)


# ---------------------------------------------------------------------------
# Mid-run crash regression (Codex P1 on PR #185)
# ---------------------------------------------------------------------------


def test_mid_run_crash_with_partial_trades_fails_not_succeeds(monkeypatch) -> None:
    """A service error after earlier fills must NOT pass through as success.

    ``TradingService.run`` appends closed trades to ``result.trades`` *before*
    the harness raises ``StrategyRuntimeError``, so a partial ledger can
    coexist with ``service_result.error``. The legacy guard (``error and
    not trades``) would have treated this as success with truncated data
    — a path that could promote a broken strategy to paper/live.
    """
    partial: List[TradeRecord] = [_partial_trade()]
    diagnostics = BacktestExecutionDiagnostics(
        zero_trade_category="ORDERS_REJECTED",
        summary="3 of 4 orders rejected by risk limits before crash",
        orders_emitted=4,
        orders_rejected=3,
    )
    _patch_run_backtest(
        monkeypatch,
        service_result=TradingServiceResult(
            trades=partial,
            error="IndexError: list index out of range\n<traceback>",
            lookahead_violation=False,
            execution_diagnostics=diagnostics,
        ),
    )

    result = sandbox_compat.run_strategy_code("dummy", {}, _config(), strategy=_strategy())

    assert result.success is False, "partial crash must fail the run so refinement / 422 paths fire"
    assert result.error_type == "runtime_error"
    assert "IndexError" in (result.stderr or "")
    # Partial trades are carried through for diagnostic visibility.
    assert result.trades == partial
    # Service-level diagnostics propagate on the runtime-error path so #413/#414
    # can route the failure on top of evidence rather than a generic message.
    assert result.execution_diagnostics is diagnostics


def test_lookahead_violation_with_partial_trades_still_classified_as_lookahead(
    monkeypatch,
) -> None:
    """A lookahead_violation with prior fills keeps its own error_type."""
    partial: List[TradeRecord] = [_partial_trade()]
    diagnostics = BacktestExecutionDiagnostics(
        summary="lookahead detected mid-run",
        bars_processed=42,
    )
    _patch_run_backtest(
        monkeypatch,
        service_result=TradingServiceResult(
            trades=partial,
            error="AttributeError: Bar has no 'next_close'",
            lookahead_violation=True,
            execution_diagnostics=diagnostics,
        ),
    )

    result = sandbox_compat.run_strategy_code("dummy", {}, _config(), strategy=_strategy())

    assert result.success is False
    assert result.error_type == "lookahead_violation"
    assert result.trades == partial
    assert result.execution_diagnostics is diagnostics


def test_clean_run_reports_success(monkeypatch) -> None:
    """Baseline: no error + trades present → success=True."""
    trades: List[TradeRecord] = [_partial_trade()]
    diagnostics = BacktestExecutionDiagnostics(
        summary="strategy emitted and closed 1 trade",
        bars_processed=20,
        orders_emitted=2,
        orders_accepted=2,
        entries_filled=1,
        exits_emitted=1,
        closed_trades=1,
    )
    _patch_run_backtest(
        monkeypatch,
        service_result=TradingServiceResult(
            trades=trades,
            error=None,
            lookahead_violation=False,
            execution_diagnostics=diagnostics,
        ),
    )
    result = sandbox_compat.run_strategy_code("dummy", {}, _config(), strategy=_strategy())
    assert result.success is True
    assert result.error_type is None
    assert result.trades == trades
    # Diagnostics flow through on the success path too — refinement only fires
    # on anomalies, but persisted run records (#414) want the envelope either way.
    assert result.execution_diagnostics is diagnostics


def test_initialisation_failure_with_empty_ledger_still_fails(monkeypatch) -> None:
    """Regression check — the happy path for initialisation-time errors
    (strategy module missing, subprocess startup failure) still reports
    ``success=False`` now that the guard no longer depends on
    ``not run.trades``."""
    diagnostics = BacktestExecutionDiagnostics(
        zero_trade_category="NO_ORDERS_EMITTED",
        summary="strategy module never initialised; no bars processed",
    )
    _patch_run_backtest(
        monkeypatch,
        service_result=TradingServiceResult(
            trades=[],
            error="strategy module must define a subclass of contract.Strategy",
            lookahead_violation=False,
            execution_diagnostics=diagnostics,
        ),
    )
    result = sandbox_compat.run_strategy_code("dummy", {}, _config(), strategy=_strategy())
    assert result.success is False
    assert result.error_type == "runtime_error"
    assert "strategy module" in (result.stderr or "")
    assert result.execution_diagnostics is diagnostics


def test_pre_backtest_value_error_synthesizes_unknown_zero_trade_diagnostics(
    monkeypatch,
) -> None:
    """When ``run_backtest`` raises ``ValueError`` before any service result
    exists (missing strategy_code, ambiguous market_data), the shim must
    synthesize a minimal diagnostics envelope tagged
    ``UNKNOWN_ZERO_TRADE_PATH`` so #413/#414 can still classify the failure."""

    def _raise(*_args, **_kwargs):
        raise ValueError("strategy_code is required")

    monkeypatch.setattr(sandbox_compat, "run_backtest", _raise)

    result = sandbox_compat.run_strategy_code("", {}, _config(), strategy=_strategy())

    assert result.success is False
    assert result.error_type == "runtime_error"
    assert "strategy_code is required" in (result.stderr or "")
    assert result.execution_diagnostics is not None
    assert result.execution_diagnostics.zero_trade_category == "UNKNOWN_ZERO_TRADE_PATH"
    assert "strategy_code is required" in result.execution_diagnostics.summary
    # Counter fields stay at their model defaults — no bars were processed.
    assert result.execution_diagnostics.bars_processed == 0
    assert result.execution_diagnostics.orders_emitted == 0
