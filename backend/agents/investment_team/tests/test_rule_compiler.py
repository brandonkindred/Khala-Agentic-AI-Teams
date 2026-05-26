"""Unit tests for ``executor.rule_compiler.evaluate_exit_rules`` (issue #527).

The evaluator is intentionally pure: it takes ``(rules, positions, bars)``
and returns ``ExitIntent`` records. Each test fabricates synthetic state
and asserts on the returned intents without touching the trading service.
"""

from __future__ import annotations

from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.executor.rule_compiler import (
    BarSnapshot,
    PositionState,
    evaluate_exit_rules,
)
from investment_team.strategy_lab.spec_dsl import (
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)


def _long(symbol: str = "AAA", **kwargs) -> PositionState:
    defaults = {
        "symbol": symbol,
        "side": "long",
        "qty": 100.0,
        "entry_price": 100.0,
        "high_since_entry": 100.0,
        "low_since_entry": 100.0,
    }
    defaults.update(kwargs)
    return PositionState(**defaults)


def _short(symbol: str = "AAA", **kwargs) -> PositionState:
    defaults = {
        "symbol": symbol,
        "side": "short",
        "qty": 100.0,
        "entry_price": 100.0,
        "high_since_entry": 100.0,
        "low_since_entry": 100.0,
    }
    defaults.update(kwargs)
    return PositionState(**defaults)


def _bar(high: float = 101.0, low: float = 99.0, close: float = 100.0) -> BarSnapshot:
    return BarSnapshot(high=high, low=low, close=close)


# ---------------------------------------------------------------------------
# StopLossRule — entry_price basis
# ---------------------------------------------------------------------------


def test_stop_loss_entry_price_long_fires_on_low_below_floor() -> None:
    # entry=100, pct=0.03 → floor=97. Bar low=96.5 < 97 → fires.
    pos = _long()
    rule = StopLossRule(pct=0.03)  # default basis = entry_price
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=98, low=96.5)})
    assert len(intents) == 1
    assert intents[0].rule_kind == "stop_loss"


def test_stop_loss_entry_price_long_does_not_fire_when_low_above_floor() -> None:
    pos = _long()
    rule = StopLossRule(pct=0.03)
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(low=97.5)})
    assert intents == []


def test_stop_loss_entry_price_long_fires_at_exact_floor() -> None:
    pos = _long()
    rule = StopLossRule(pct=0.05)  # floor=95
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(low=95.0)})
    assert len(intents) == 1


def test_stop_loss_entry_price_short_fires_on_high_above_ceiling() -> None:
    # short, entry=100, pct=0.03 → ceiling=103. Bar high=103.5 > 103 → fires.
    pos = _short()
    rule = StopLossRule(pct=0.03)
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=103.5, low=99)})
    assert len(intents) == 1


def test_stop_loss_entry_price_short_does_not_fire_when_high_below_ceiling() -> None:
    pos = _short()
    rule = StopLossRule(pct=0.03)
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=102, low=99)})
    assert intents == []


# ---------------------------------------------------------------------------
# StopLossRule — trailing variants
# ---------------------------------------------------------------------------


def test_stop_loss_trailing_high_long_uses_watermark() -> None:
    # entry=100, but the position has run up to a high of 110 since entry.
    # pct=0.05 → trailing floor = 110 * 0.95 = 104.5. Bar low=104 < 104.5 → fires.
    pos = _long(high_since_entry=110.0)
    rule = StopLossRule(pct=0.05, basis="trailing_high")
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=105, low=104)})
    assert len(intents) == 1
    assert intents[0].rule_kind == "stop_loss"


def test_stop_loss_trailing_high_long_no_fire_when_no_run_up() -> None:
    # Watermark == entry → trailing floor = entry * (1 - pct), same as entry basis.
    pos = _long(high_since_entry=100.0)
    rule = StopLossRule(pct=0.05, basis="trailing_high")
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(low=96)})
    assert intents == []


def test_stop_loss_trailing_high_inapplicable_to_short_is_noop() -> None:
    # ``trailing_high`` is the long-side counterpart; using it on a short
    # must NOT silently flush the position. The rule is a no-op for shorts.
    pos = _short(high_since_entry=110.0)
    rule = StopLossRule(pct=0.05, basis="trailing_high")
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=120, low=99)})
    assert intents == []


def test_stop_loss_trailing_low_short_uses_watermark() -> None:
    # short, entry=100, drawn down to low=90. pct=0.05 → trailing ceiling =
    # 90 * 1.05 = 94.5. Bar high=95 > 94.5 → fires.
    pos = _short(low_since_entry=90.0)
    rule = StopLossRule(pct=0.05, basis="trailing_low")
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=95, low=88)})
    assert len(intents) == 1


def test_stop_loss_trailing_low_inapplicable_to_long_is_noop() -> None:
    pos = _long(low_since_entry=90.0)
    rule = StopLossRule(pct=0.05, basis="trailing_low")
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(low=70)})
    assert intents == []


# ---------------------------------------------------------------------------
# TakeProfitRule
# ---------------------------------------------------------------------------


def test_take_profit_long_fires_on_high_above_target() -> None:
    pos = _long()
    rule = TakeProfitRule(pct=0.05)  # target = 105
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=105.5, low=102)})
    assert len(intents) == 1
    assert intents[0].rule_kind == "take_profit"


def test_take_profit_long_does_not_fire_below_target() -> None:
    pos = _long()
    rule = TakeProfitRule(pct=0.05)
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=104.9)})
    assert intents == []


def test_take_profit_short_fires_on_low_below_target() -> None:
    pos = _short()
    rule = TakeProfitRule(pct=0.05)  # target = 95 (profit when short and price falls)
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar(high=98, low=94.5)})
    assert len(intents) == 1


# ---------------------------------------------------------------------------
# Multiple rules — first-by-spec-order wins
# ---------------------------------------------------------------------------


def test_first_triggered_rule_wins_when_both_fire() -> None:
    pos = _long()
    rules = [StopLossRule(pct=0.05), TakeProfitRule(pct=0.05)]
    # Stop-loss fires (low=94 < 95 floor). Take-profit also fires (high=106 > 105).
    intents = evaluate_exit_rules(rules, {"AAA": pos}, {"AAA": _bar(high=106, low=94)})
    assert len(intents) == 1
    # ``stop_loss`` first in spec → it wins.
    assert intents[0].rule_kind == "stop_loss"
    assert intents[0].rule_index == 0


def test_rule_order_swapped_changes_winner() -> None:
    pos = _long()
    rules = [TakeProfitRule(pct=0.05), StopLossRule(pct=0.05)]
    intents = evaluate_exit_rules(rules, {"AAA": pos}, {"AAA": _bar(high=106, low=94)})
    assert len(intents) == 1
    assert intents[0].rule_kind == "take_profit"


def test_no_rules_yields_no_intents() -> None:
    intents = evaluate_exit_rules([], {"AAA": _long()}, {"AAA": _bar()})
    assert intents == []


def test_no_open_positions_yields_no_intents() -> None:
    intents = evaluate_exit_rules([StopLossRule(pct=0.05)], {}, {"AAA": _bar()})
    assert intents == []


def test_zero_qty_position_skipped() -> None:
    pos = _long(qty=0.0)
    intents = evaluate_exit_rules([StopLossRule(pct=0.05)], {"AAA": pos}, {"AAA": _bar(low=90)})
    assert intents == []


def test_position_without_matching_bar_skipped() -> None:
    pos = _long()
    intents = evaluate_exit_rules([StopLossRule(pct=0.05)], {"AAA": pos}, {})
    assert intents == []


# ---------------------------------------------------------------------------
# Helpers for HistoryView-backed tests
# ---------------------------------------------------------------------------


def _build_view(closes: list[float], symbol: str = "AAA") -> StreamingHistoryView:
    view = StreamingHistoryView()
    for i, c in enumerate(closes):
        view.append(
            BarRecord(
                timestamp=f"2024-01-{i + 1:02d}",
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1000.0,
            )
        )
    return view


# ---------------------------------------------------------------------------
# SignalExitRule — no-op without views, enforced with views
# ---------------------------------------------------------------------------


def test_signal_exit_rule_noop_without_views() -> None:
    pos = _long()
    rule = SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=90.0))
    intents = evaluate_exit_rules([rule], {"AAA": pos}, {"AAA": _bar()})
    assert intents == []


def test_signal_exit_fires_with_view_when_predicate_satisfied() -> None:
    pos = _long()
    rule = SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=90.0))
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    intents = evaluate_exit_rules(
        [rule],
        {"AAA": pos},
        {"AAA": _bar(close=100.0)},
        views=views,
    )
    assert len(intents) == 1
    assert intents[0].rule_kind == "signal_exit"


def test_signal_exit_does_not_fire_when_predicate_not_satisfied() -> None:
    pos = _long()
    rule = SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=200.0))
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    intents = evaluate_exit_rules(
        [rule],
        {"AAA": pos},
        {"AAA": _bar(close=100.0)},
        views=views,
    )
    assert intents == []


def test_signal_exit_warmup_returns_none() -> None:
    pos = _long()
    rule = SignalExitRule(
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 50}),
            op=">",
            rhs=90.0,
        )
    )
    views = {"AAA": _build_view([100.0, 101.0, 102.0])}
    intents = evaluate_exit_rules(
        [rule],
        {"AAA": pos},
        {"AAA": _bar()},
        views=views,
    )
    assert intents == []


def test_signal_exit_does_not_block_other_rules_in_spec() -> None:
    pos = _long()
    signal = SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=200.0))
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    intents = evaluate_exit_rules(
        [signal, StopLossRule(pct=0.05)],
        {"AAA": pos},
        {"AAA": _bar(low=94)},
        views=views,
    )
    assert len(intents) == 1
    assert intents[0].rule_kind == "stop_loss"
    assert intents[0].rule_index == 1


# ---------------------------------------------------------------------------
# Multi-symbol — each position evaluated independently
# ---------------------------------------------------------------------------


def test_multi_symbol_evaluates_each_position_against_its_own_bar() -> None:
    rules = [StopLossRule(pct=0.05)]
    positions = {
        "AAA": _long("AAA"),
        "BBB": _long("BBB"),
    }
    # AAA bar trips the stop floor (low=94 < 95); BBB stays above (low=96).
    bars = {"AAA": _bar(low=94), "BBB": _bar(low=96)}
    intents = evaluate_exit_rules(rules, positions, bars)
    assert len(intents) == 1
    assert intents[0].symbol == "AAA"


def test_multi_symbol_skips_when_no_bar_for_symbol() -> None:
    rules = [StopLossRule(pct=0.05)]
    positions = {
        "AAA": _long("AAA"),
        "BBB": _long("BBB"),
    }
    # Both would fire by bar alone, but BBB has no bar — only AAA emits.
    bars = {"AAA": _bar(low=94)}
    intents = evaluate_exit_rules(rules, positions, bars)
    assert len(intents) == 1
    assert intents[0].symbol == "AAA"
