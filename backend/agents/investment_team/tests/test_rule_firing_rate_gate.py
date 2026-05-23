"""Unit tests for :class:`RuleFiringRateGate`."""

from __future__ import annotations

from typing import List, Optional

from investment_team.models import StrategySpec, TradeRecord
from investment_team.strategy_lab.quality_gates.realism.rule_firing import (
    GATE,
    RuleFiringRateGate,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    SignalExitRule,
    StopLossRule,
)


def _spec(
    *,
    entry_rules: Optional[List[EntryRule]] = None,
    exit_rules: Optional[list] = None,
    requires_custom_code: bool = False,
) -> StrategySpec:
    return StrategySpec(
        strategy_id="rule-firing-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=entry_rules
        or [EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=exit_rules or [StopLossRule(pct=0.05)],
        risk_limits={},
        speculative=False,
        requires_custom_code=requires_custom_code,
        strategy_code="pass",
    )


def _trade(
    trade_num: int,
    *,
    entry_reason: Optional[str] = None,
    exit_reason: Optional[str] = None,
) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date=f"2024-{((trade_num % 12) + 1):02d}-15",
        exit_date=f"2024-{((trade_num % 12) + 1):02d}-20",
        symbol="QQQ",
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=10.0,
        net_pnl=10.0,
        return_pct=1.0,
        hold_days=5,
        outcome="win",
        cumulative_pnl=10.0 * trade_num,
        entry_reason=entry_reason,
        exit_reason=exit_reason,
    )


def _criticals(results):
    return [r for r in results if not r.passed and r.severity == "critical"]


def _warnings(results):
    return [r for r in results if not r.passed and r.severity == "warning"]


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


def test_skips_when_requires_custom_code():
    gate = RuleFiringRateGate()
    spec = _spec(requires_custom_code=True)
    trades = [_trade(1, entry_reason="compiled_entry:entry[0]")]
    results = gate.check(spec, trades)
    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)
    assert "custom_code" in results[0].details.lower()


# ---------------------------------------------------------------------------
# Entry rule firing
# ---------------------------------------------------------------------------


def test_critical_when_entry_rule_never_fires():
    """Spec declares one entry rule, no trade's entry_reason cites it →
    critical (dead-code entry)."""
    gate = RuleFiringRateGate()
    spec = _spec(
        entry_rules=[
            EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0)),
        ]
    )
    trades = [_trade(i + 1, entry_reason=None) for i in range(20)]
    results = gate.check(spec, trades)
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "entry[0]" in criticals[0].details
    assert criticals[0].gate_name == GATE
    assert criticals[0].rule_id == "entry[0]"


def test_critical_for_each_unfired_entry_rule():
    """Two entry rules, only entry[1] fires → critical on entry[0]."""
    gate = RuleFiringRateGate()
    spec = _spec(
        entry_rules=[
            EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0)),
            EntryRule(side="short", when=Predicate(lhs="bar.close", op="<", rhs=0)),
        ]
    )
    trades = [_trade(i + 1, entry_reason="compiled_entry:entry[1]") for i in range(10)]
    results = gate.check(spec, trades)
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "entry[0]" in criticals[0].details
    assert criticals[0].rule_id == "entry[0]"


def test_passes_when_all_entry_rules_fire():
    gate = RuleFiringRateGate()
    spec = _spec(
        entry_rules=[
            EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0)),
            EntryRule(side="short", when=Predicate(lhs="bar.close", op="<", rhs=0)),
        ]
    )
    trades = [
        _trade(1, entry_reason="compiled_entry:entry[0]"),
        _trade(2, entry_reason="compiled_entry:entry[1]"),
    ]
    results = gate.check(spec, trades)
    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed for r in results)
    assert "fired at least once" in results[0].details


# ---------------------------------------------------------------------------
# Signal-exit rule firing
# ---------------------------------------------------------------------------


def test_warning_for_unfired_signal_exit_rule():
    """A SignalExitRule that never fired should be a warning, not critical,
    because stop-loss / take-profit exits may legitimately supersede it."""
    gate = RuleFiringRateGate()
    spec = _spec(
        exit_rules=[
            SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0)),
            StopLossRule(pct=0.05),
        ]
    )
    trades = [
        _trade(i + 1, entry_reason="compiled_entry:entry[0]", exit_reason="engine_exit:stop_loss")
        for i in range(10)
    ]
    results = gate.check(spec, trades)
    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "exit[0]" in warnings[0].details
    assert warnings[0].rule_id == "exit[0]"
    assert _criticals(results) == []


def test_no_warning_for_unfired_stop_loss_rule():
    """StopLossRule is not a SignalExitRule — the gate only tracks signal
    exits; mechanical exits are the engine's responsibility."""
    gate = RuleFiringRateGate()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    trades = [_trade(i + 1, entry_reason="compiled_entry:entry[0]") for i in range(10)]
    results = gate.check(spec, trades)
    assert _warnings(results) == []
    assert _criticals(results) == []


def test_signal_exit_passes_when_it_fires():
    gate = RuleFiringRateGate()
    spec = _spec(
        exit_rules=[
            SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0)),
        ]
    )
    trades = [
        _trade(
            1, entry_reason="compiled_entry:entry[0]", exit_reason="compiled_signal_exit:exit[0]"
        ),
    ]
    results = gate.check(spec, trades)
    assert _warnings(results) == []
    assert _criticals(results) == []


# ---------------------------------------------------------------------------
# Trades without annotations (legacy / non-compiler path)
# ---------------------------------------------------------------------------


def test_no_entry_reason_on_any_trade_fires_critical_for_each_entry_rule():
    """When no trade carries an entry_reason (e.g. pre-annotation legacy
    runs), every entry rule reports zero firings → one critical per rule."""
    gate = RuleFiringRateGate()
    spec = _spec()
    trades = [_trade(i + 1) for i in range(10)]
    results = gate.check(spec, trades)
    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "entry[0]" in criticals[0].details


def test_compiler_reason_format_matching():
    """Only exact ``compiled_entry:entry[N]`` patterns match — freeform
    reasons like ``"compiled_entry"`` (the old un-indexed format) don't
    increment any rule's count."""
    gate = RuleFiringRateGate()
    spec = _spec()
    trades = [_trade(i + 1, entry_reason="compiled_entry") for i in range(10)]
    results = gate.check(spec, trades)
    criticals = _criticals(results)
    assert len(criticals) == 1


def test_substring_reason_does_not_match():
    """Prefixed/suffixed reasons that merely *contain* the token must not
    count — full-string match only."""
    gate = RuleFiringRateGate()
    spec = _spec(
        exit_rules=[
            SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0)),
        ]
    )
    trades = [
        _trade(1, entry_reason="compiled_entry:entry[0]", exit_reason="prefix_compiled_signal_exit:exit[0]"),
        _trade(2, entry_reason="compiled_entry:entry[0]", exit_reason="compiled_signal_exit:exit[0]_suffix"),
        _trade(3, entry_reason="extra_compiled_entry:entry[0]", exit_reason="compiled_signal_exit:exit[0]"),
    ]
    results = gate.check(spec, trades)
    # trade 3's entry_reason is prefixed → doesn't count for entry[0]
    # Only trade 1 and 2 have valid entry_reason ("compiled_entry:entry[0]")
    # For exit: only trade 3 has exact match
    warnings = _warnings(results)
    assert len(warnings) == 0
    assert _criticals(results) == []
