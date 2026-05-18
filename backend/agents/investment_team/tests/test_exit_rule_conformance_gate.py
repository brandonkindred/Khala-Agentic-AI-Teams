"""Unit tests for ``ExitRuleConformanceGate`` (issue #527).

Drives the gate with hand-built ``TradeRecord`` / diagnostics fixtures so
each rule type's check is exercised in isolation, then a final happy-path
test runs the whole gate end-to-end against a multi-rule spec.
"""

from __future__ import annotations

from typing import List

from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    TradeRecord,
)
from investment_team.strategy_lab.quality_gates.exit_rule_conformance import (
    GATE,
    ExitRuleConformanceGate,
)
from investment_team.strategy_lab.spec_dsl import (
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    TimeStopRule,
)


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        slippage_bps=2.0,
    )


def _trade(
    *,
    trade_num: int = 1,
    hold_days: int = 5,
    return_pct: float = 1.0,
    side: str = "long",
    entry_price: float = 100.0,
    exit_price: float = 101.0,
) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date="2024-01-01",
        exit_date="2024-01-06",
        symbol="AAA",
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        shares=100.0,
        position_value=entry_price * 100.0,
        gross_pnl=(exit_price - entry_price) * 100.0,
        net_pnl=(exit_price - entry_price) * 100.0,
        return_pct=return_pct,
        hold_days=hold_days,
        outcome="win" if return_pct > 0 else "loss",
        cumulative_pnl=(exit_price - entry_price) * 100.0,
    )


def _diagnostics(**firings: int) -> BacktestExecutionDiagnostics:
    return BacktestExecutionDiagnostics(exit_rule_firings=dict(firings))


# ---------------------------------------------------------------------------
# Empty exit_rules
# ---------------------------------------------------------------------------


def test_no_exit_rules_skips_with_info() -> None:
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[],
        trades=[_trade()],
        diagnostics=_diagnostics(),
        config=_config(),
    )
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].severity == "info"
    assert "empty" in results[0].details.lower()


# ---------------------------------------------------------------------------
# TimeStopRule
# ---------------------------------------------------------------------------


def test_time_stop_pass_within_ceiling() -> None:
    gate = ExitRuleConformanceGate()
    trades = [_trade(hold_days=h) for h in (1, 3, 5)]
    results = gate.check(
        exit_rules=[TimeStopRule(n_bars=5)],
        trades=trades,
        diagnostics=_diagnostics(time_stop=2),
        config=_config(),
    )
    # First result is the TimeStop check; gate may append an aggregate info row.
    time_stop = [r for r in results if "TimeStopRule" in r.details]
    assert all(r.passed for r in time_stop), [r.details for r in time_stop]


def test_time_stop_fail_when_trade_exceeds_ceiling() -> None:
    gate = ExitRuleConformanceGate()
    # n_bars=5 → ceiling = 5 + 1 = 6. hold_days=7 violates.
    trades = [_trade(hold_days=h) for h in (1, 7, 4)]
    results = gate.check(
        exit_rules=[TimeStopRule(n_bars=5)],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fail = [r for r in results if not r.passed]
    assert len(fail) == 1
    assert fail[0].severity == "critical"
    assert "TimeStopRule" in fail[0].details
    assert fail[0].gate_name == GATE


def test_time_stop_skipped_on_sub_daily_timeframe() -> None:
    gate = ExitRuleConformanceGate()
    trades = [_trade(hold_days=99)]  # would fail on daily
    results = gate.check(
        exit_rules=[TimeStopRule(n_bars=5)],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
        timeframe="15m",
    )
    # No critical violations; just info "skipped on sub-daily".
    fails = [r for r in results if not r.passed]
    assert fails == []
    skipped = [r for r in results if "skipped" in r.details and "15m" in r.details]
    assert skipped


def test_time_stop_ceiling_includes_one_bar_fill_lag() -> None:
    # n_bars=10 → ceiling=11. hold_days==11 should pass (not violate).
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[TimeStopRule(n_bars=10)],
        trades=[_trade(hold_days=11)],
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


# ---------------------------------------------------------------------------
# StopLossRule
# ---------------------------------------------------------------------------


def test_stop_loss_entry_price_pass_within_tolerance() -> None:
    gate = ExitRuleConformanceGate()
    # pct=0.03 → -3%. Slippage 2bps → 2x slip = 4bps = 0.04% extra each side.
    # Tolerance band: -3.0 - 0.04 - 0.5 ≈ -3.54%. -3.5% should pass.
    trades = [_trade(return_pct=-3.5)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.03)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in results]


def test_stop_loss_entry_price_fail_when_below_floor() -> None:
    gate = ExitRuleConformanceGate()
    # pct=0.03 → floor ≈ -3.54%. -10% is well below.
    trades = [_trade(return_pct=-10.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.03)],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    assert "StopLossRule" in fails[0].details


def test_stop_loss_trailing_variant_skipped() -> None:
    gate = ExitRuleConformanceGate()
    # Trailing variants can't be checked from the ledger alone.
    trades = [_trade(return_pct=-50.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.03, basis="trailing_high")],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


# ---------------------------------------------------------------------------
# TakeProfitRule
# ---------------------------------------------------------------------------


def test_take_profit_alone_fires_when_threshold_reached() -> None:
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=6.0)]  # >= 5% target
    results = gate.check(
        exit_rules=[TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(take_profit=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


def test_take_profit_alone_warns_when_never_reached() -> None:
    gate = ExitRuleConformanceGate()
    # take_profit is the only rule, no trade reached 5% — warning.
    trades = [_trade(return_pct=1.0), _trade(trade_num=2, return_pct=2.0)]
    results = gate.check(
        exit_rules=[TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "warning"


def test_take_profit_with_other_rules_is_informational() -> None:
    # With another rule present, never reaching the take-profit threshold
    # is acceptable (the other rule may close trades first).
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=1.0)]
    results = gate.check(
        exit_rules=[TimeStopRule(n_bars=10), TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(time_stop=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


# ---------------------------------------------------------------------------
# SignalExitRule
# ---------------------------------------------------------------------------


def test_signal_exit_rule_is_informational_only() -> None:
    gate = ExitRuleConformanceGate()
    rule = SignalExitRule(
        when=Predicate(
            lhs=IndicatorRef(name="rsi", params={"period": 14}),
            op=">",
            rhs=70.0,
        )
    )
    results = gate.check(
        exit_rules=[rule],
        trades=[_trade()],
        diagnostics=_diagnostics(),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []
    # The note appears in one of the info rows.
    assert any("SignalExitRule" in r.details for r in results)


# ---------------------------------------------------------------------------
# Diagnostics summary row
# ---------------------------------------------------------------------------


def test_diagnostics_summary_includes_firings_breakdown() -> None:
    gate = ExitRuleConformanceGate()
    diag = _diagnostics(time_stop=3, stop_loss=1)
    results = gate.check(
        exit_rules=[TimeStopRule(n_bars=10)],
        trades=[_trade(hold_days=5)],
        diagnostics=diag,
        config=_config(),
    )
    summary = [r for r in results if "engine_exits" in r.details]
    assert summary, [r.details for r in results]
    assert "time_stop=3" in summary[0].details
    assert "stop_loss=1" in summary[0].details


def test_diagnostics_summary_handles_no_firings() -> None:
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[TimeStopRule(n_bars=10)],
        trades=[_trade(hold_days=5)],
        diagnostics=_diagnostics(),
        config=_config(),
    )
    summary = [r for r in results if "engine_exits" in r.details]
    assert summary
    assert "none" in summary[0].details


# ---------------------------------------------------------------------------
# Multi-rule integration
# ---------------------------------------------------------------------------


def test_multi_rule_all_pass_returns_no_failures() -> None:
    gate = ExitRuleConformanceGate()
    trades: List[TradeRecord] = [
        _trade(trade_num=1, hold_days=3, return_pct=-2.0),
        _trade(trade_num=2, hold_days=5, return_pct=1.0),
        _trade(trade_num=3, hold_days=2, return_pct=6.0),
    ]
    results = gate.check(
        exit_rules=[
            TimeStopRule(n_bars=10),
            StopLossRule(pct=0.05),
            TakeProfitRule(pct=0.05),
        ],
        trades=trades,
        diagnostics=_diagnostics(time_stop=1, stop_loss=1, take_profit=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in fails]
