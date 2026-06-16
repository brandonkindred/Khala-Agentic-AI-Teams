"""Regression tests for the execution-diagnostics → metrics → conformance-gate wiring.

Guards the defect where ``ExitRuleConformanceGate`` failed conformance on runs
the engine had actually enforced correctly: ``compute_metrics`` builds
``BacktestResult`` from the trade ledger alone and leaves
``execution_diagnostics=None``, while the engine's per-symbol exit-rule firing
counters live only on the ``StrategyRunResult``. The orchestrator never carried
those counters onto ``metrics``, so the gate (which reads
``metrics.execution_diagnostics``) saw zero firings and flagged every
engine-attributed below-floor stop-loss trade as an unaccounted leak.

These tests pin down:
  * ``_attach_execution_diagnostics`` copies the engine counters onto metrics
    (and never overwrites a populated value with ``None``).
  * A real engine-stopped backtest passes conformance once diagnostics are
    attached — and the firing telemetry is load-bearing (an empty-but-present
    diagnostics envelope still trips the leak check).
  * The gate is fail-safe when diagnostics are entirely absent: missing
    telemetry is informational, never a critical veto.
"""

from __future__ import annotations

import textwrap
from typing import Dict, List, Optional

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    StrategySpec,
    TradeRecord,
)
from investment_team.strategy_lab._orchestrator_helpers import (
    _attach_execution_diagnostics,
)
from investment_team.strategy_lab.quality_gates.exit_rule_conformance import (
    ExitRuleConformanceGate,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    StopLossRule,
)
from investment_team.trade_simulator import compute_metrics
from investment_team.trading_service.modes.sandbox_compat import (
    StrategyRunResult,
    run_strategy_code,
)

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_engine_exit_enforcement so the engine fires a stop and
# the realised next-bar fill lands below the raw -5% floor).
# ---------------------------------------------------------------------------


def _mk_bar(date_iso: str, close: float, *, high: float = None, low: float = None) -> OHLCVBar:
    return OHLCVBar(
        date=date_iso,
        open=close - 0.2,
        high=close + 0.5 if high is None else high,
        low=close - 0.5 if low is None else low,
        close=close,
        volume=1_000_000,
    )


def _falling_bars(
    n_pre_drop: int = 5, drop_bar_low: float = 90.0, n_post_drop: int = 10
) -> Dict[str, List[OHLCVBar]]:
    """Flat → big single-bar drop → flat. The drop bar's low pierces a
    pct=0.05 stop floor (95) against entry 100; the next-bar fill (~92) lands
    below the raw -5% floor, so the trade is an engine_exit:stop_loss close
    realised beneath the floor.
    """
    out: List[OHLCVBar] = []
    base = 100.0
    for i in range(n_pre_drop):
        out.append(_mk_bar(f"2024-01-{i + 1:02d}", base))
    drop_day = n_pre_drop + 1
    out.append(
        OHLCVBar(
            date=f"2024-01-{drop_day:02d}",
            open=base - 0.5,
            high=base,
            low=drop_bar_low,
            close=base - 8.0,
            volume=1_000_000,
        )
    )
    for i in range(n_post_drop):
        day = drop_day + 1 + i
        out.append(_mk_bar(f"2024-01-{day:02d}", base - 8.0))
    return {"AAA": out}


_ENTRY_ONLY_STRATEGY = textwrap.dedent(
    '''\
    """Open one long, never exit. Engine enforcement is on its own."""
    from contract import OrderSide, OrderType, Strategy


    class EntryOnly(Strategy):
        def on_bar(self, ctx, bar):
            if ctx.position(bar.symbol) is not None:
                return
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=10,
                order_type=OrderType.MARKET,
                reason="entry_only",
            )
    '''
)


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-02-15",
        initial_capital=100_000.0,
        slippage_bps=2.0,
        transaction_cost_bps=5.0,
    )


def _spec(*, exit_rules) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-exit-conformance-wiring",
        authored_by="tests",
        asset_class="equity",
        hypothesis="engine-side exit rules close positions strategy_code leaves open",
        signal_definition="enter long once, leave to the engine to close",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close",
                    op=">",
                    rhs=IndicatorRef(name="sma", params={"period": 5}),
                ),
            )
        ],
        exit_rules=exit_rules,
        strategy_code=_ENTRY_ONLY_STRATEGY,
    )


def _below_floor_engine_stop_trade(
    *,
    trade_num: int = 1,
    return_pct: float = -7.0,
    exit_reason: Optional[str] = "engine_exit:stop_loss",
) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date="2024-01-06",
        exit_date="2024-01-07",
        symbol="AAA",
        side="long",
        entry_price=100.0,
        exit_price=100.0 + return_pct,
        shares=10.0,
        position_value=1_000.0,
        gross_pnl=return_pct * 10.0,
        net_pnl=return_pct * 10.0,
        return_pct=return_pct,
        hold_days=1,
        outcome="loss",
        cumulative_pnl=return_pct * 10.0,
        exit_reason=exit_reason,
    )


# ---------------------------------------------------------------------------
# Helper: _attach_execution_diagnostics
# ---------------------------------------------------------------------------


def test_attach_execution_diagnostics_copies_when_present() -> None:
    cfg = _config()
    metrics = compute_metrics([], cfg.initial_capital, cfg.start_date, cfg.end_date)
    assert metrics.execution_diagnostics is None  # the gap compute_metrics leaves

    diag = BacktestExecutionDiagnostics(
        exit_rule_firings={"stop_loss": 1},
        exit_rule_firings_by_symbol={"AAA": {"stop_loss": 1}},
    )
    exec_result = StrategyRunResult(success=True, trades=[], execution_diagnostics=diag)

    _attach_execution_diagnostics(metrics=metrics, exec_result=exec_result)

    assert metrics.execution_diagnostics is diag


def test_attach_execution_diagnostics_noop_when_absent() -> None:
    cfg = _config()
    metrics = compute_metrics([], cfg.initial_capital, cfg.start_date, cfg.end_date)
    existing = BacktestExecutionDiagnostics(exit_rule_firings={"stop_loss": 2})
    metrics.execution_diagnostics = existing

    exec_result = StrategyRunResult(success=True, trades=[], execution_diagnostics=None)
    _attach_execution_diagnostics(metrics=metrics, exec_result=exec_result)

    # A populated value is never clobbered with None.
    assert metrics.execution_diagnostics is existing


# ---------------------------------------------------------------------------
# End-to-end: engine-stopped run passes conformance once diagnostics are wired.
# ---------------------------------------------------------------------------


def test_engine_stopped_run_passes_conformance_only_with_real_firings() -> None:
    """A real backtest where the engine fires a stop-loss below the floor must
    pass conformance once the engine's firing counters are carried onto
    ``metrics`` — and must FAIL if those counters are missing (proving the
    firing telemetry, not the fail-safe, is what clears the run).
    """
    cfg = _config()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    market_data = _falling_bars(n_pre_drop=5, drop_bar_low=90.0, n_post_drop=10)

    exec_result = run_strategy_code(spec.strategy_code, market_data, cfg, strategy=spec)
    assert exec_result.success, exec_result.error_type
    trades = exec_result.trades

    # The engine fired and closed below the raw -5% floor.
    diag = exec_result.execution_diagnostics
    assert diag is not None
    assert diag.exit_rule_firings.get("stop_loss", 0) >= 1, diag.exit_rule_firings
    below_floor_engine_stops = [
        t for t in trades if t.exit_reason == "engine_exit:stop_loss" and t.return_pct < -5.0
    ]
    assert below_floor_engine_stops, [(t.exit_reason, t.return_pct) for t in trades]

    # compute_metrics leaves diagnostics unset — the bug precondition.
    metrics = compute_metrics(trades, cfg.initial_capital, cfg.start_date, cfg.end_date)
    assert metrics.execution_diagnostics is None

    gate = ExitRuleConformanceGate()

    # With an empty-but-present diagnostics envelope (zero firings), the leak
    # check correctly fires a critical — the firing counts are load-bearing.
    results_empty = gate.check(
        exit_rules=spec.exit_rules,
        trades=trades,
        diagnostics=BacktestExecutionDiagnostics(),
        config=cfg,
    )
    criticals_empty = [r for r in results_empty if not r.passed and r.severity == "critical"]
    assert criticals_empty, [r.details for r in results_empty]

    # After attaching the real firing counters, conformance passes.
    _attach_execution_diagnostics(metrics=metrics, exec_result=exec_result)
    assert metrics.execution_diagnostics is diag

    results = gate.check(
        exit_rules=spec.exit_rules,
        trades=trades,
        diagnostics=metrics.execution_diagnostics,
        config=cfg,
    )
    criticals = [r for r in results if not r.passed and r.severity == "critical"]
    assert criticals == [], [r.details for r in results]


# ---------------------------------------------------------------------------
# Fail-safe: absent telemetry must not manufacture a critical veto.
# ---------------------------------------------------------------------------


def test_gate_none_diagnostics_does_not_veto() -> None:
    gate = ExitRuleConformanceGate()
    trades = [_below_floor_engine_stop_trade(return_pct=-7.6)]

    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=None,
        config=_config(),
    )

    criticals = [r for r in results if not r.passed and r.severity == "critical"]
    assert criticals == [], [r.details for r in results]
    assert any("telemetry unavailable" in r.details for r in results), [r.details for r in results]
