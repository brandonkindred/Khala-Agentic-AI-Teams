"""Tests for the laddered (scaled) take-profit DSL and its pure evaluator.

Covers:

* ``ScaledTakeProfitRule`` validation — strictly-increasing rung ``pct``,
  ``sum(qty_fraction) <= 1.0`` (with the boundary 1.0 accepted), per-field
  bounds, and the ``_SpecNode`` non-finite-float guard.
* ``_format_rule`` prose rendering and ``ExitRuleAdapter`` JSON round-trip
  (the designer authors these as structured JSON, so the discriminated union
  must accept and re-emit the new kind).
* ``evaluate_exit_rules`` expansion into one ``ExitIntent`` per crossed rung,
  carrying ``level_index`` / ``qty_fraction``, for both long and short, and the
  ``first_only`` cap.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from investment_team.strategy_lab.executor.rule_compiler import (
    BarSnapshot,
    PositionState,
    evaluate_exit_rules,
)
from investment_team.strategy_lab.spec_dsl import (
    ExitRuleAdapter,
    ScaledTakeProfitRule,
    StopLossRule,
    format_rules_for_prompt,
)


def _ladder(**kw) -> ScaledTakeProfitRule:
    levels = kw.pop(
        "levels",
        [{"pct": 0.05, "qty_fraction": 0.5}, {"pct": 0.10, "qty_fraction": 0.3}],
    )
    return ScaledTakeProfitRule(levels=levels, **kw)


def _long(entry_price: float = 100.0) -> PositionState:
    return PositionState(
        symbol="AAA",
        side="long",
        qty=100,
        entry_price=entry_price,
        high_since_entry=entry_price,
        low_since_entry=entry_price,
    )


def _short(entry_price: float = 100.0) -> PositionState:
    return PositionState(
        symbol="AAA",
        side="short",
        qty=100,
        entry_price=entry_price,
        high_since_entry=entry_price,
        low_since_entry=entry_price,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_ladder_round_trips_through_adapter() -> None:
    rule = _ladder()
    restored = ExitRuleAdapter.validate_json(rule.model_dump_json())
    assert isinstance(restored, ScaledTakeProfitRule)
    assert restored.kind == "scaled_take_profit"
    assert [(level.pct, level.qty_fraction) for level in restored.levels] == [
        (0.05, 0.5),
        (0.10, 0.3),
    ]


def test_fractions_summing_to_exactly_one_are_accepted() -> None:
    rule = _ladder(
        levels=[
            {"pct": 0.05, "qty_fraction": 0.5},
            {"pct": 0.10, "qty_fraction": 0.3},
            {"pct": 0.15, "qty_fraction": 0.2},
        ]
    )
    assert math.isclose(sum(level.qty_fraction for level in rule.levels), 1.0)


def test_fractions_summing_above_one_are_rejected() -> None:
    with pytest.raises(ValidationError, match="sum to <= 1.0"):
        _ladder(
            levels=[
                {"pct": 0.05, "qty_fraction": 0.6},
                {"pct": 0.10, "qty_fraction": 0.6},
            ]
        )


def test_non_increasing_pct_is_rejected() -> None:
    with pytest.raises(ValidationError, match="strictly increasing pct"):
        _ladder(
            levels=[
                {"pct": 0.10, "qty_fraction": 0.3},
                {"pct": 0.05, "qty_fraction": 0.3},
            ]
        )


def test_equal_pct_rungs_are_rejected() -> None:
    with pytest.raises(ValidationError, match="strictly increasing pct"):
        _ladder(
            levels=[
                {"pct": 0.05, "qty_fraction": 0.3},
                {"pct": 0.05, "qty_fraction": 0.3},
            ]
        )


def test_empty_levels_rejected() -> None:
    with pytest.raises(ValidationError):
        ScaledTakeProfitRule(levels=[])


@pytest.mark.parametrize(
    "level",
    [
        {"pct": 0.0, "qty_fraction": 0.5},  # pct must be > 0
        {"pct": 0.05, "qty_fraction": 0.0},  # qty_fraction must be > 0
        {"pct": 0.05, "qty_fraction": 1.5},  # qty_fraction must be <= 1.0
    ],
)
def test_per_field_bounds_rejected(level: dict) -> None:
    with pytest.raises(ValidationError):
        ScaledTakeProfitRule(levels=[level])


def test_non_finite_pct_rejected() -> None:
    with pytest.raises(ValidationError):
        ScaledTakeProfitRule(levels=[{"pct": math.inf, "qty_fraction": 0.5}])


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_rule_renders_each_rung() -> None:
    assert format_rules_for_prompt([_ladder()]) == "scaled take profit (50% at 5%, 30% at 10%)"


# ---------------------------------------------------------------------------
# Evaluator expansion
# ---------------------------------------------------------------------------


def test_long_bar_crossing_both_rungs_emits_two_intents_in_order() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=111.0, low=100.0, close=110.0)  # clears +5% and +10%
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=False)
    assert [(i.rule_kind, i.level_index, i.qty_fraction) for i in intents] == [
        ("scaled_take_profit", 0, 0.5),
        ("scaled_take_profit", 1, 0.3),
    ]


def test_long_bar_crossing_only_first_rung_emits_one_intent() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=106.0, low=100.0, close=105.0)  # clears +5% only
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=False)
    assert [i.level_index for i in intents] == [0]


def test_short_bar_crossing_both_rungs() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=100.0, low=89.0, close=90.0)  # clears -5% and -10%
    intents = evaluate_exit_rules([rule], {"AAA": _short()}, {"AAA": bar}, first_only=False)
    assert [i.level_index for i in intents] == [0, 1]


def test_first_only_caps_scaled_intents_at_one() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=111.0, low=100.0, close=110.0)
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=True)
    assert [i.level_index for i in intents] == [0]


def test_untriggered_ladder_emits_nothing() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=103.0, low=99.0, close=101.0)  # below the +5% target
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=False)
    assert intents == []


def test_stop_loss_priority_when_listed_before_ladder() -> None:
    # A bar that trips both the stop floor (97) and the +5% target. With the stop
    # listed first, spec priority returns it ahead of the scale-out rung.
    rules = [StopLossRule(pct=0.03), _ladder()]
    bar = BarSnapshot(high=106.0, low=96.0, close=100.0)
    intents = evaluate_exit_rules(
        [rules[0], rules[1]], {"AAA": _long()}, {"AAA": bar}, first_only=False
    )
    assert intents[0].rule_kind == "stop_loss"
    assert any(i.rule_kind == "scaled_take_profit" for i in intents)
