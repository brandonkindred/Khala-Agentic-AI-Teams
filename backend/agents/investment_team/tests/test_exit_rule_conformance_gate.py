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
    ExitRuleConformanceGate,
)
from investment_team.strategy_lab.spec_dsl import (
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
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
# StopLossRule
# ---------------------------------------------------------------------------


def test_stop_loss_passes_when_engine_fired_even_on_gap_fill() -> None:
    """The engine detects the trigger on bar N's low but fills on bar N+1's
    open. Overnight gaps can push the realised return well past the raw
    floor without indicating an enforcement bug, so the gate must pass
    as long as the engine emitted at least one stop_loss close.
    """
    gate = ExitRuleConformanceGate()
    # pct=0.05 → raw floor=-5%. Trade closed at -20% (huge gap) — but the
    # engine recorded a stop_loss firing, so this is a real-world gap
    # fill, not a leak.
    trades = [_trade(return_pct=-20.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in results]


def test_stop_loss_fails_when_below_floor_and_engine_never_fired() -> None:
    """Critical only when trades cleared the raw floor AND the engine
    recorded no stop_loss firings — i.e. the rule should have triggered
    but the enforcement path didn't run.
    """
    gate = ExitRuleConformanceGate()
    # pct=0.05 → raw floor=-5%. -10% trade with zero engine firings:
    # the engine missed the trigger, which is a real leak.
    trades = [_trade(return_pct=-10.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(),  # no firings
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    assert "StopLossRule" in fails[0].details
    assert "leak" in fails[0].details.lower()


def test_stop_loss_passes_when_below_floor_but_engine_fired() -> None:
    """Same below-floor trade, but engine recorded a firing → gap fill,
    not a leak. Conformance should pass.
    """
    gate = ExitRuleConformanceGate()
    trades = [_trade(return_pct=-10.0)]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


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
        exit_rules=[StopLossRule(pct=0.05), TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
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
    diag = _diagnostics(stop_loss=3, take_profit=1)
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=[_trade(return_pct=-1.0)],
        diagnostics=diag,
        config=_config(),
    )
    summary = [r for r in results if "engine_exits" in r.details]
    assert summary, [r.details for r in results]
    assert "stop_loss=3" in summary[0].details
    assert "take_profit=1" in summary[0].details


def test_diagnostics_summary_handles_no_firings() -> None:
    gate = ExitRuleConformanceGate()
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=[_trade(return_pct=1.0)],
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
        _trade(trade_num=1, return_pct=-2.0),
        _trade(trade_num=2, return_pct=1.0),
        _trade(trade_num=3, return_pct=6.0),
    ]
    results = gate.check(
        exit_rules=[
            StopLossRule(pct=0.05),
            TakeProfitRule(pct=0.05),
        ],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1, take_profit=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in fails]


def test_stop_loss_fails_when_below_floor_trades_exceed_firings() -> None:
    """Per-trade leak counting: one correctly stopped trade must NOT mask
    a separate trade that closed below the floor without an engine firing.
    Regression for the P2 review comment on issue #527 PR: the previous
    "any firings > 0 → pass" check was too loose.
    """
    gate = ExitRuleConformanceGate()
    # Two below-floor trades, only one engine firing → 1 unaccounted leak.
    trades = [
        _trade(trade_num=1, return_pct=-7.0),
        _trade(trade_num=2, return_pct=-8.0),
    ]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert len(fails) == 1
    assert fails[0].severity == "critical"
    assert "leak" in fails[0].details.lower()
    assert "1 unaccounted-for trade" in fails[0].details


def test_stop_loss_passes_when_firings_cover_below_floor_count() -> None:
    """Exactly as many firings as below-floor trades → no leak. Each
    below-floor trade is plausibly accounted for by a matching engine
    emission; gap-fill semantics absorb the rest.
    """
    gate = ExitRuleConformanceGate()
    trades = [
        _trade(trade_num=1, return_pct=-7.0),
        _trade(trade_num=2, return_pct=-8.0),
    ]
    results = gate.check(
        exit_rules=[StopLossRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(stop_loss=2),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == []


def test_take_profit_alone_passes_on_firings_even_with_short_realized_return() -> None:
    """Realized return below the rule's raw target is expected when the
    engine fires take-profit on bar N's high but the synthetic close
    fills on bar N+1's open (gap / slippage / costs eat into the
    realised return). The gate must use the engine firings diagnostic,
    not the trade ledger's ``return_pct``, to verify enforcement.
    Regression for the P3 review comment on issue #527 PR.
    """
    gate = ExitRuleConformanceGate()
    # take_profit target = 5%; trade realised 3% after gap/costs.
    trades = [_trade(return_pct=3.0)]
    results = gate.check(
        exit_rules=[TakeProfitRule(pct=0.05)],
        trades=trades,
        diagnostics=_diagnostics(take_profit=1),
        config=_config(),
    )
    fails = [r for r in results if not r.passed]
    assert fails == [], [r.details for r in results]


def test_take_profit_alone_warns_when_engine_never_fired() -> None:
    """Take-profit is the only rule, trades exist, but engine recorded
    zero firings — the threshold may be unreachable on the strategy's
    universe. Surface as a warning so the operator can revisit the
    spec.
    """
    gate = ExitRuleConformanceGate()
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
    assert "zero take_profit firings" in fails[0].details
