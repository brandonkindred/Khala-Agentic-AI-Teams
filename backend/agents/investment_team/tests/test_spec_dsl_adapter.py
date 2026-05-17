"""Tests for the spec_dsl_adapter module (issue #537 literal schema)."""

from __future__ import annotations

import pytest

from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    TimeStopRule,
    VolatilityTargetSizing,
    format_rules_for_prompt,
    format_sizing_rule,
)
from investment_team.strategy_lab.spec_dsl_adapter import (
    parse_entry_rule,
    parse_exit_rule,
    parse_rule_list,
    parse_sizing_list,
    parse_sizing_rule,
)


def _sma(period: int) -> IndicatorRef:
    return IndicatorRef(name="sma", params={"period": period})


def _ema(period: int, source: str = "close") -> IndicatorRef:
    return IndicatorRef(name="ema", params={"period": period}, source=source)


def _rsi(period: int = 14) -> IndicatorRef:
    return IndicatorRef(name="rsi", params={"period": period})


# ---------------------------------------------------------------------------
# Entry-rule patterns.
# ---------------------------------------------------------------------------


def test_entry_close_gt_sma():
    parsed = parse_entry_rule("close > sma(20)")
    assert isinstance(parsed, EntryRule)
    assert parsed.side == "long"
    assert parsed.when == Predicate(lhs="bar.close", op=">", rhs=_sma(20))


def test_entry_rsi_lt_30():
    parsed = parse_entry_rule("RSI < 30")
    assert isinstance(parsed, EntryRule)
    assert parsed.when == Predicate(lhs=_rsi(), op="<", rhs=30.0)


def test_entry_short_when_rsi_gt_70():
    parsed = parse_entry_rule("short when rsi(14) > 70")
    assert isinstance(parsed, EntryRule)
    assert parsed.side == "short"
    assert parsed.when == Predicate(lhs=_rsi(14), op=">", rhs=70.0)


def test_entry_crosses_above():
    parsed = parse_entry_rule("sma(20) crosses above sma(50)")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.op == "cross_above"
    assert parsed.when.lhs == _sma(20)
    assert parsed.when.rhs == _sma(50)


def test_entry_unparsable_returns_none():
    parsed = parse_entry_rule("enter on bullish momentum")
    assert parsed is None


# ---------------------------------------------------------------------------
# Exit-rule patterns.
# ---------------------------------------------------------------------------


def test_exit_when_rsi_gt_70():
    parsed = parse_exit_rule("exit when RSI > 70")
    assert isinstance(parsed, SignalExitRule)
    assert parsed.when == Predicate(lhs=_rsi(14), op=">", rhs=70.0)


@pytest.mark.parametrize(
    "prose,expected_bars",
    [
        ("exit after 5 bars", 5),
        ("exit after 10 days", 10),
        ("exit after 7 periods", 7),
    ],
)
def test_exit_time_stop(prose, expected_bars):
    parsed = parse_exit_rule(prose)
    assert isinstance(parsed, TimeStopRule)
    assert parsed.n_bars == expected_bars


@pytest.mark.parametrize(
    "prose,expected_pct",
    [
        ("stop loss 3%", 0.03),
        ("stop-loss: 0.03", 0.03),
        ("stop loss: 5%", 0.05),
    ],
)
def test_exit_stop_loss(prose, expected_pct):
    parsed = parse_exit_rule(prose)
    assert isinstance(parsed, StopLossRule)
    assert parsed.pct == pytest.approx(expected_pct)


@pytest.mark.parametrize(
    "prose,expected_pct",
    [
        ("take profit 5%", 0.05),
        ("target 5%", 0.05),
        ("take-profit: 10%", 0.10),
    ],
)
def test_exit_take_profit(prose, expected_pct):
    parsed = parse_exit_rule(prose)
    assert isinstance(parsed, TakeProfitRule)
    assert parsed.pct == pytest.approx(expected_pct)


def test_exit_unparsable_returns_none():
    parsed = parse_exit_rule("vibes")
    assert parsed is None


# ---------------------------------------------------------------------------
# Sizing patterns.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prose,expected_fraction",
    [
        ("risk 2% per trade", 0.02),
        ("allocate 2% per trade", 0.02),
        ("risk 1.5% per trade", 0.015),
    ],
)
def test_sizing_fixed_fraction(prose, expected_fraction):
    parsed = parse_sizing_rule(prose)
    assert isinstance(parsed, FixedFractionSizing)
    assert parsed.fraction == pytest.approx(expected_fraction)


@pytest.mark.parametrize(
    "prose,expected_vol",
    [
        ("vol-target 10%", 0.10),
        ("volatility-target 10%", 0.10),
        ("vol target 12.5%", 0.125),
    ],
)
def test_sizing_vol_target(prose, expected_vol):
    parsed = parse_sizing_rule(prose)
    assert isinstance(parsed, VolatilityTargetSizing)
    assert parsed.target_annual_vol == pytest.approx(expected_vol)


@pytest.mark.parametrize(
    "prose,expected_usd",
    [
        ("$50000 per trade", 50000),
        ("$50000 notional", 50000),
        ("$1000.50 per trade", 1000.50),
    ],
)
def test_sizing_fixed_notional(prose, expected_usd):
    parsed = parse_sizing_rule(prose)
    assert isinstance(parsed, FixedNotionalSizing)
    assert parsed.notional_usd == pytest.approx(expected_usd)


def test_sizing_unparsable_returns_none():
    parsed = parse_sizing_rule("size up if confident")
    assert parsed is None


# ---------------------------------------------------------------------------
# parse_rule_list / parse_sizing_list collapse semantics.
# ---------------------------------------------------------------------------


def test_parse_rule_list_entry():
    parsed = parse_rule_list(["close > sma(20)", "vibes"], kind="entry")
    assert len(parsed) == 2
    assert isinstance(parsed[0], EntryRule)
    assert parsed[1] is None


def test_parse_rule_list_exit():
    parsed = parse_rule_list(["stop loss 3%", "exit after 5 bars"], kind="exit")
    assert isinstance(parsed[0], StopLossRule)
    assert isinstance(parsed[1], TimeStopRule)


def test_sizing_list_empty_returns_none():
    assert parse_sizing_list([]) is None


def test_sizing_list_single():
    parsed = parse_sizing_list(["risk 2% per trade"])
    assert isinstance(parsed, FixedFractionSizing)
    assert parsed.fraction == pytest.approx(0.02)


def test_sizing_list_collapse_first_wins_with_note():
    parsed = parse_sizing_list(["risk 2% per trade", "max 5% gross"])
    assert isinstance(parsed, FixedFractionSizing)
    assert parsed.fraction == pytest.approx(0.02)
    assert parsed.note == "max 5% gross"


def test_sizing_list_collapse_skips_unparsable_then_chooses():
    parsed = parse_sizing_list(["size on vibes", "risk 2% per trade", "max 5% gross"])
    assert isinstance(parsed, FixedFractionSizing)
    assert parsed.fraction == pytest.approx(0.02)
    # Both the unparseable leader and the trailing constraint land in note.
    assert "size on vibes" in parsed.note
    assert "max 5% gross" in parsed.note


def test_sizing_list_all_unparsable_returns_none():
    assert parse_sizing_list(["foo", "bar"]) is None


# ---------------------------------------------------------------------------
# Formatter <-> adapter round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=_sma(20))),
        EntryRule(side="short", when=Predicate(lhs=_rsi(14), op=">", rhs=70.0)),
    ],
)
def test_entry_formatter_round_trip(rule):
    rendered = format_rules_for_prompt([rule])
    reparsed = parse_entry_rule(rendered)
    assert isinstance(reparsed, EntryRule)
    assert reparsed.side == rule.side
    assert reparsed.when == rule.when


@pytest.mark.parametrize(
    "rule",
    [
        TimeStopRule(n_bars=5),
        StopLossRule(pct=0.03),
        StopLossRule(pct=0.03, basis="trailing_high"),
        StopLossRule(pct=0.03, basis="trailing_low"),
        TakeProfitRule(pct=0.05),
        SignalExitRule(when=Predicate(lhs=_rsi(14), op=">", rhs=70.0)),
    ],
)
def test_exit_formatter_round_trip(rule):
    rendered = format_rules_for_prompt([rule])
    reparsed = parse_exit_rule(rendered)
    assert type(reparsed) is type(rule)
    assert reparsed.model_dump(exclude={"note"}) == rule.model_dump(exclude={"note"})


@pytest.mark.parametrize(
    "rule",
    [
        FixedFractionSizing(fraction=0.02),
        VolatilityTargetSizing(target_annual_vol=0.10),
        FixedNotionalSizing(notional_usd=50000),
    ],
)
def test_sizing_formatter_round_trip(rule):
    rendered = format_sizing_rule(rule)
    reparsed = parse_sizing_rule(rendered)
    assert type(reparsed) is type(rule)
    assert reparsed.model_dump(exclude={"note"}) == rule.model_dump(exclude={"note"})


# ---------------------------------------------------------------------------
# Regression tests carried forward from PR #558.
# ---------------------------------------------------------------------------


def test_entry_enter_when_legacy_phrasing():
    """Legacy specs use `enter when …`; the adapter must accept that wording."""
    parsed = parse_entry_rule("enter when RSI < 30")
    assert isinstance(parsed, EntryRule)
    assert parsed.side == "long"
    assert parsed.when == Predicate(lhs=_rsi(14), op="<", rhs=30.0)


def test_indicator_source_preserved_in_round_trip():
    rule = EntryRule(
        side="long",
        when=Predicate(lhs=_ema(50, source="open"), op=">", rhs="bar.close"),
    )
    rendered = format_rules_for_prompt([rule])
    assert "source=open" in rendered
    reparsed = parse_entry_rule(rendered)
    assert isinstance(reparsed, EntryRule)
    assert reparsed.when.lhs == _ema(50, source="open")


def test_indicator_source_kwarg_parses():
    parsed = parse_entry_rule("ema(50, source=open) > close")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.lhs == _ema(50, source="open")


def test_trailing_stop_loss_round_trip_high():
    rule = StopLossRule(pct=0.03, basis="trailing_high")
    rendered = format_rules_for_prompt([rule])
    assert rendered == "trailing-high stop loss 3%"
    reparsed = parse_exit_rule(rendered)
    assert isinstance(reparsed, StopLossRule)
    assert reparsed.basis == "trailing_high"
    assert reparsed.pct == pytest.approx(0.03)


def test_trailing_stop_loss_round_trip_low():
    rule = StopLossRule(pct=0.05, basis="trailing_low")
    rendered = format_rules_for_prompt([rule])
    assert rendered == "trailing-low stop loss 5%"
    reparsed = parse_exit_rule(rendered)
    assert isinstance(reparsed, StopLossRule)
    assert reparsed.basis == "trailing_low"


def test_indicator_extra_positional_args_rejected():
    """`sma(20,50) > close` had silently dropped the 50; now → None."""
    assert parse_entry_rule("sma(20,50) > close") is None


def test_macd_extra_positional_args_rejected():
    assert parse_entry_rule("macd(12,26,9,99) > 0") is None


def test_indicator_unknown_kwarg_rejected():
    assert parse_entry_rule("sma(20, foo=bar) > close") is None


def test_atr_source_kwarg_rejected():
    """ATR has no `source` field; specifying one must reject."""
    assert parse_exit_rule("exit when atr(14, source=open) > 0") is None


# ---------------------------------------------------------------------------
# Bare default-argument indicators in predicates.
# ---------------------------------------------------------------------------


def test_predicate_bare_adx():
    parsed = parse_entry_rule("ADX > 25")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.lhs == IndicatorRef(name="adx", params={"period": 14})
    assert parsed.when.rhs == 25.0


def test_predicate_bare_macd():
    parsed = parse_entry_rule("macd > 0")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.lhs == IndicatorRef(
        name="macd", params={"fast": 12, "slow": 26, "signal": 9, "output": "macd"}
    )
    assert parsed.when.rhs == 0.0


def test_predicate_bare_stochastic_k():
    parsed = parse_entry_rule("stochastic_k < 20")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.lhs == IndicatorRef(
        name="stochastic", params={"k_period": 14, "d_period": 3, "output": "k"}
    )


def test_predicate_bare_sma_stays_unparsable():
    """SMA/EMA still need an explicit period — bare `sma > x` should fail."""
    assert parse_entry_rule("sma > close") is None


# ---------------------------------------------------------------------------
# Enter prefix with explicit side.
# ---------------------------------------------------------------------------


def test_entry_enter_short_when():
    parsed = parse_entry_rule("enter short when rsi > 70")
    assert isinstance(parsed, EntryRule)
    assert parsed.side == "short"
    assert parsed.when == Predicate(lhs=_rsi(14), op=">", rhs=70.0)


def test_entry_enter_long_when():
    parsed = parse_entry_rule("enter long when close > sma(20)")
    assert isinstance(parsed, EntryRule)
    assert parsed.side == "long"
    assert parsed.when == Predicate(lhs="bar.close", op=">", rhs=_sma(20))


# ---------------------------------------------------------------------------
# Hyphenated indicator aliases.
# ---------------------------------------------------------------------------


def test_hyphenated_indicator_alias_call():
    parsed = parse_entry_rule("macd-signal(12,26,9) > 0")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.lhs == IndicatorRef(
        name="macd", params={"fast": 12, "slow": 26, "signal": 9, "output": "signal"}
    )


def test_hyphenated_indicator_alias_bare():
    parsed = parse_entry_rule("stochastic-k < 20")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.lhs == IndicatorRef(
        name="stochastic", params={"k_period": 14, "d_period": 3, "output": "k"}
    )


# ---------------------------------------------------------------------------
# Case-insensitive source value.
# ---------------------------------------------------------------------------


def test_source_kwarg_case_insensitive():
    parsed = parse_entry_rule("EMA(50, SOURCE=OPEN) > CLOSE")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.lhs == _ema(50, source="open")


# ---------------------------------------------------------------------------
# MACD default-output formatter round-trip.
# ---------------------------------------------------------------------------


def test_macd_default_output_round_trip():
    macd = IndicatorRef(name="macd", params={"fast": 12, "slow": 26, "signal": 9, "output": "macd"})
    rule = EntryRule(side="long", when=Predicate(lhs=macd, op=">", rhs=0.0))
    rendered = format_rules_for_prompt([rule])
    # The default output ("macd") must format as bare `macd(...)`, not
    # `macd_macd(...)`, otherwise the adapter can't reparse it.
    assert "macd_macd" not in rendered
    reparsed = parse_entry_rule(rendered)
    assert isinstance(reparsed, EntryRule)
    assert reparsed.when.lhs == macd


# ---------------------------------------------------------------------------
# Empty positional slots.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prose",
    ["sma(,20) > close", "macd(12,,9) > 0", "sma(20,) > close"],
)
def test_indicator_empty_positional_slot_rejected(prose):
    """`sma(,20)` etc. used to silently drop the empty slot and shift values."""
    assert parse_entry_rule(prose) is None


# ---------------------------------------------------------------------------
# Singular `cross` operator.
# ---------------------------------------------------------------------------


def test_singular_cross_above():
    parsed = parse_entry_rule("sma(20) cross above sma(50)")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.op == "cross_above"


def test_singular_cross_below():
    parsed = parse_entry_rule("sma(20) cross below sma(50)")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.op == "cross_below"


# ---------------------------------------------------------------------------
# Leading-dot numeric thresholds.
# ---------------------------------------------------------------------------


def test_predicate_leading_dot_number():
    parsed = parse_entry_rule("rsi < .5")
    assert isinstance(parsed, EntryRule)
    assert parsed.when.rhs == 0.5


# ---------------------------------------------------------------------------
# Formatter ↔ adapter round-trip across the full float range.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [1e-13, 1e-06, 0.0001, 0.5, 100.5, 1e20],
)
def test_small_decimal_round_trip(value):
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=float(value)),
    )
    rendered = format_rules_for_prompt([rule])
    reparsed = parse_entry_rule(rendered)
    assert isinstance(reparsed, EntryRule)
    assert reparsed.when.rhs == pytest.approx(value)


@pytest.mark.parametrize("value", [5e-10, 1e-12, 1e-15])
def test_tiny_const_not_collapsed_to_zero(value):
    rendered = format_rules_for_prompt(
        [EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=float(value)))]
    )
    # Must not collapse to the bare `0` integer.
    assert not rendered.endswith("> 0")
    reparsed = parse_entry_rule(rendered)
    assert isinstance(reparsed, EntryRule)
    assert reparsed.when.rhs == pytest.approx(value)


def test_tiny_fixed_fraction_round_trip():
    rule = FixedFractionSizing(fraction=1e-12)
    rendered = format_sizing_rule(rule)
    # Must not collapse to the literal "risk 0% per trade" prose, which the
    # adapter would round-trip into a Pydantic `gt=0` failure.
    assert rendered != "risk 0% per trade"
    reparsed = parse_sizing_rule(rendered)
    assert isinstance(reparsed, FixedFractionSizing)
    assert reparsed.fraction == pytest.approx(1e-12)


def test_tiny_vol_target_round_trip():
    rule = VolatilityTargetSizing(target_annual_vol=1e-10)
    rendered = format_sizing_rule(rule)
    reparsed = parse_sizing_rule(rendered)
    assert isinstance(reparsed, VolatilityTargetSizing)
    assert reparsed.target_annual_vol == pytest.approx(1e-10)


# ---------------------------------------------------------------------------
# Every float-bearing DSL node rejects non-finite values.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "factory",
    [
        lambda v: StopLossRule(pct=v),
        lambda v: TakeProfitRule(pct=v),
        lambda v: FixedFractionSizing(fraction=v),
        lambda v: VolatilityTargetSizing(target_annual_vol=v),
        lambda v: FixedNotionalSizing(notional_usd=v),
    ],
)
def test_dsl_nodes_reject_non_finite_floats(factory, bad):
    from pydantic import ValidationError as _VE

    with pytest.raises(_VE):
        factory(bad)


def test_bollinger_num_std_rejects_non_finite():
    from pydantic import ValidationError as _VE

    with pytest.raises(_VE):
        IndicatorRef(name="bollinger", params={"num_std": float("inf")})
