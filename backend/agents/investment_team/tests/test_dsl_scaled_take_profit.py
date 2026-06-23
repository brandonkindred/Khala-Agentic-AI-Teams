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
    evaluate_exit_rules_for_position,
)
from investment_team.strategy_lab.spec_dsl import (
    ExitRuleAdapter,
    ScaledTakeProfitRule,
    StopLossRule,
    format_rules_for_prompt,
    is_full_position_exit,
    is_partial_exit,
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


def test_ladder_offers_only_cursor_rung() -> None:
    # The evaluator offers AT MOST one intent per ladder — the next un-fired rung
    # (the cursor) — even on a bar that clears several targets at once. With no
    # cursor supplied the cursor defaults to 0.
    rule = _ladder()
    bar = BarSnapshot(high=111.0, low=100.0, close=110.0)  # clears +5% and +10%
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=False)
    assert [(i.rule_kind, i.level_index, i.qty_fraction) for i in intents] == [
        ("scaled_take_profit", 0, 0.5),
    ]


def test_full_close_ladder_is_classified_as_full_position_exit() -> None:
    # A ladder whose rungs sum to 1.0 fully closes the position over its rungs, so
    # the rule-level classifiers treat it as a full-position exit, not partial —
    # whether it is a single 1.0 rung or several rungs summing to 1.0.
    single = _ladder(levels=[{"pct": 0.05, "qty_fraction": 1.0}])
    multi = _ladder(levels=[{"pct": 0.05, "qty_fraction": 0.5}, {"pct": 0.10, "qty_fraction": 0.5}])
    for full in (single, multi):
        assert is_full_position_exit(full) is True
        assert is_partial_exit(full) is False
    # A ladder summing to < 1.0 leaves a residual → partial, not full.
    partial = _ladder(levels=[{"pct": 0.05, "qty_fraction": 0.5}])
    assert is_full_position_exit(partial) is False
    assert is_partial_exit(partial) is True


def test_full_close_rung_intent_is_still_a_scaled_rung() -> None:
    # A qty_fraction == 1.0 rung is still a SCALED rung at the intent level (it flows
    # through the scale-out path and is sized off the original qty); the engine, not
    # this flag, decides full-vs-partial cleanups from the resulting close qty.
    rule = _ladder(levels=[{"pct": 0.05, "qty_fraction": 1.0}])
    bar = BarSnapshot(high=106.0, low=100.0, close=105.0)  # +5% reached
    [intent] = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar})
    assert intent.is_scaled_rung is True
    assert intent.qty_fraction == 1.0


def test_exclude_scaled_drops_scaled_rungs_but_keeps_full_exits() -> None:
    # exclude_scaled defers ALL scaled rungs (incl. a 1.0 rung — it must be sized off
    # the settled original qty), but never drops a non-scaled full-position exit.
    bar = BarSnapshot(high=106.0, low=96.0, close=98.0)  # +5% rung AND -3% stop
    pos = _long()
    for ladder in (
        _ladder(levels=[{"pct": 0.05, "qty_fraction": 0.5}]),  # partial
        _ladder(levels=[{"pct": 0.05, "qty_fraction": 1.0}]),  # full-close rung
    ):
        rules = [ladder, StopLossRule(pct=0.03)]
        # Without exclusion the higher-priority rung (rule 0) wins.
        kept = evaluate_exit_rules_for_position(rules, "AAA", pos, bar)
        assert kept[0].is_scaled_rung is True
        # With exclude_scaled the rung is skipped and the stop (rule 1) fires.
        deferred = evaluate_exit_rules_for_position(rules, "AAA", pos, bar, exclude_scaled=True)
        assert [i.rule_kind for i in deferred] == ["stop_loss"]


def test_cursor_selects_the_next_unfired_rung() -> None:
    # As rungs fire, the dispatcher advances the cursor; the evaluator then offers
    # the next rung. Walk the cursor across the ladder and off the end.
    rule = _ladder()
    bar = BarSnapshot(high=111.0, low=100.0, close=110.0)  # both targets reached
    pos = {"AAA": _long()}

    def rungs(cursor: int):
        intents = evaluate_exit_rules(
            [rule], pos, {"AAA": bar}, first_only=False, scaled_cursors={"AAA": {0: cursor}}
        )
        return [i.level_index for i in intents]

    assert rungs(0) == [0]
    assert rungs(1) == [1]  # rung 0 fired → cursor 1 offers rung 1
    assert rungs(2) == []  # ladder exhausted


def test_long_bar_crossing_only_first_rung_offers_rung0() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=106.0, low=100.0, close=105.0)  # clears +5% only
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=False)
    assert [i.level_index for i in intents] == [0]
    # Cursor at rung 1, whose +10% target this bar has NOT reached → nothing.
    intents = evaluate_exit_rules(
        [rule], {"AAA": _long()}, {"AAA": bar}, first_only=False, scaled_cursors={"AAA": {0: 1}}
    )
    assert intents == []


def test_short_cursor_walks_the_ladder() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=100.0, low=89.0, close=90.0)  # clears -5% and -10%
    pos = {"AAA": _short()}
    assert [i.level_index for i in evaluate_exit_rules([rule], pos, {"AAA": bar})] == [0]
    i1 = evaluate_exit_rules([rule], pos, {"AAA": bar}, scaled_cursors={"AAA": {0: 1}})
    assert [i.level_index for i in i1] == [1]


def test_first_only_caps_scaled_intents_at_one() -> None:
    rule = _ladder()
    bar = BarSnapshot(high=111.0, low=100.0, close=110.0)
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=True)
    assert [i.level_index for i in intents] == [0]


def test_untriggered_ladder_emits_nothing() -> None:
    rule = _ladder()
    # Neither the current bar nor the since-entry watermark has reached +5%.
    bar = BarSnapshot(high=103.0, low=99.0, close=101.0)
    intents = evaluate_exit_rules([rule], {"AAA": _long()}, {"AAA": bar}, first_only=False)
    assert intents == []


def test_cursor_rung_stays_eligible_via_watermark_after_retrace_long() -> None:
    # A gap bar cleared rung 1's target (peak 111); rung 0 already fired so the
    # cursor is at 1. A later bar retraces below +10%, but eligibility is
    # high-water-mark based, so the cursor rung stays eligible.
    rule = _ladder()  # rungs at +5% (105) and +10% (110)
    pos = PositionState(
        symbol="AAA",
        side="long",
        qty=100,
        entry_price=100.0,
        high_since_entry=111.0,  # peak since entry cleared rung 1's target
        low_since_entry=100.0,
    )
    bar = BarSnapshot(high=104.0, low=102.0, close=103.0)  # now below +10%
    intents = evaluate_exit_rules(
        [rule], {"AAA": pos}, {"AAA": bar}, first_only=False, scaled_cursors={"AAA": {0: 1}}
    )
    assert [i.level_index for i in intents] == [1]


def test_cursor_rung_stays_eligible_via_watermark_after_retrace_short() -> None:
    rule = _ladder()  # rungs at -5% (95) and -10% (90)
    pos = PositionState(
        symbol="AAA",
        side="short",
        qty=100,
        entry_price=100.0,
        high_since_entry=100.0,
        low_since_entry=89.0,  # trough since entry cleared rung 1's target
    )
    bar = BarSnapshot(high=98.0, low=96.0, close=97.0)  # now above -10%
    intents = evaluate_exit_rules(
        [rule], {"AAA": pos}, {"AAA": bar}, first_only=False, scaled_cursors={"AAA": {0: 1}}
    )
    assert [i.level_index for i in intents] == [1]


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
