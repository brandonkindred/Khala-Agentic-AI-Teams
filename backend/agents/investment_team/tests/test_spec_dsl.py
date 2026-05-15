"""Tests for the spec_dsl module (issue #550, step 1 of 8 from #537)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from investment_team.strategy_lab.spec_dsl import (
    ADXRef,
    ATRRef,
    BollingerRef,
    ConstRef,
    EMARef,
    EntryRule,
    EntryRuleAdapter,
    ExitRuleAdapter,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRefAdapter,
    MACDRef,
    Predicate,
    PriceRef,
    RSIRef,
    SignalExitRule,
    SizingRuleAdapter,
    SMARef,
    StochasticRef,
    StopLossRule,
    TakeProfitRule,
    TimeStopRule,
    UnparsableRule,
    UnparsableSizing,
    VolatilityTargetSizing,
    VWAPRef,
    format_rules_for_prompt,
    format_sizing_rule,
)

# ---------------------------------------------------------------------------
# Round-trip serialisation per union member.
# ---------------------------------------------------------------------------


_INDICATOR_FIXTURES = [
    PriceRef(field="close"),
    PriceRef(field="high"),
    ConstRef(value=30),
    SMARef(period=20),
    EMARef(period=50, source="open"),
    RSIRef(period=14),
    MACDRef(fast=12, slow=26, signal=9, output="signal"),
    BollingerRef(period=20, num_std=2.0, band="upper"),
    ATRRef(period=14),
    ADXRef(period=14),
    StochasticRef(k_period=14, d_period=3, output="k"),
    VWAPRef(),
]


@pytest.mark.parametrize("model", _INDICATOR_FIXTURES)
def test_indicator_round_trip(model):
    dumped = model.model_dump_json()
    rebuilt = IndicatorRefAdapter.validate_json(dumped)
    assert rebuilt == model


def test_entry_rule_round_trip():
    rule = EntryRule(
        side="long",
        when=Predicate(lhs=PriceRef(field="close"), op="gt", rhs=SMARef(period=20)),
        note="trend-follow",
    )
    rebuilt = EntryRuleAdapter.validate_json(rule.model_dump_json())
    assert rebuilt == rule


def test_unparsable_entry_round_trip():
    rule = UnparsableRule(prose="enter on vibes", reason="no pattern matched")
    rebuilt = EntryRuleAdapter.validate_json(rule.model_dump_json())
    assert rebuilt == rule


@pytest.mark.parametrize(
    "rule",
    [
        TimeStopRule(n_bars=5),
        StopLossRule(pct=0.03),
        StopLossRule(pct=0.03, basis="trailing_high"),
        TakeProfitRule(pct=0.05),
        SignalExitRule(when=Predicate(lhs=RSIRef(period=14), op="gt", rhs=ConstRef(value=70))),
        UnparsableRule(prose="exit on vibes"),
    ],
)
def test_exit_rule_round_trip(rule):
    rebuilt = ExitRuleAdapter.validate_json(rule.model_dump_json())
    assert rebuilt == rule


@pytest.mark.parametrize(
    "rule",
    [
        FixedFractionSizing(fraction=0.02),
        VolatilityTargetSizing(target_annual_vol=0.10),
        FixedNotionalSizing(notional_usd=50000),
        UnparsableSizing(prose="size based on confidence"),
    ],
)
def test_sizing_round_trip(rule):
    rebuilt = SizingRuleAdapter.validate_json(rule.model_dump_json())
    assert rebuilt == rule


# ---------------------------------------------------------------------------
# Discriminator dispatch — raw dicts route to the right concrete class.
# ---------------------------------------------------------------------------


def test_indicator_discriminator_dispatch():
    assert isinstance(IndicatorRefAdapter.validate_python({"kind": "rsi"}), RSIRef)
    assert isinstance(IndicatorRefAdapter.validate_python({"kind": "sma", "period": 20}), SMARef)
    assert isinstance(IndicatorRefAdapter.validate_python({"kind": "macd"}), MACDRef)


def test_entry_discriminator_dispatch():
    payload = {
        "kind": "entry",
        "side": "long",
        "when": {
            "lhs": {"kind": "price", "field": "close"},
            "op": "gt",
            "rhs": {"kind": "sma", "period": 20},
        },
    }
    parsed = EntryRuleAdapter.validate_python(payload)
    assert isinstance(parsed, EntryRule)
    assert isinstance(parsed.when.rhs, SMARef)


def test_exit_discriminator_dispatch():
    assert isinstance(
        ExitRuleAdapter.validate_python({"kind": "time_stop", "n_bars": 5}),
        TimeStopRule,
    )
    assert isinstance(
        ExitRuleAdapter.validate_python({"kind": "stop_loss", "pct": 0.03}),
        StopLossRule,
    )
    assert isinstance(
        ExitRuleAdapter.validate_python({"kind": "unparsable", "prose": "..."}),
        UnparsableRule,
    )


def test_sizing_discriminator_dispatch():
    assert isinstance(
        SizingRuleAdapter.validate_python({"kind": "fixed_fraction", "fraction": 0.02}),
        FixedFractionSizing,
    )
    assert isinstance(
        SizingRuleAdapter.validate_python({"kind": "unparsable_sizing", "prose": "x"}),
        UnparsableSizing,
    )


# ---------------------------------------------------------------------------
# Bounds-violation tests.
# ---------------------------------------------------------------------------


def test_sma_period_lower_bound():
    with pytest.raises(ValidationError):
        SMARef(period=1)


def test_sma_period_upper_bound():
    with pytest.raises(ValidationError):
        SMARef(period=401)


def test_rsi_period_upper_bound():
    with pytest.raises(ValidationError):
        RSIRef(period=201)


def test_bollinger_num_std_must_be_positive():
    with pytest.raises(ValidationError):
        BollingerRef(period=20, num_std=-1)


def test_macd_fast_lower_bound():
    with pytest.raises(ValidationError):
        MACDRef(fast=1)


def test_stop_loss_negative_pct():
    with pytest.raises(ValidationError):
        StopLossRule(pct=-0.01)


def test_stop_loss_pct_over_one():
    with pytest.raises(ValidationError):
        StopLossRule(pct=1.5)


def test_take_profit_negative_pct():
    with pytest.raises(ValidationError):
        TakeProfitRule(pct=-0.05)


def test_predicate_unknown_op():
    with pytest.raises(ValidationError):
        Predicate(
            lhs=PriceRef(field="close"),
            op="bogus",  # type: ignore[arg-type]
            rhs=SMARef(period=20),
        )


def test_time_stop_must_be_positive():
    with pytest.raises(ValidationError):
        TimeStopRule(n_bars=0)


def test_fixed_fraction_upper_bound():
    with pytest.raises(ValidationError):
        FixedFractionSizing(fraction=1.5)


def test_extra_field_is_forbidden():
    with pytest.raises(ValidationError):
        RSIRef.model_validate({"kind": "rsi", "period": 14, "foo": 1})


def test_missing_required_const_value():
    with pytest.raises(ValidationError):
        ConstRef.model_validate({"kind": "const"})


# ---------------------------------------------------------------------------
# Formatter golden strings.
# ---------------------------------------------------------------------------


def test_format_entry_close_gt_sma():
    rule = EntryRule(
        side="long",
        when=Predicate(lhs=PriceRef(field="close"), op="gt", rhs=SMARef(period=20)),
    )
    assert format_rules_for_prompt([rule]) == "long when close > sma(20)"


def test_format_entry_rsi_lt_30():
    rule = EntryRule(
        side="long",
        when=Predicate(lhs=RSIRef(period=14), op="lt", rhs=ConstRef(value=30)),
    )
    assert format_rules_for_prompt([rule]) == "long when rsi(14) < 30"


def test_format_time_stop():
    assert format_rules_for_prompt([TimeStopRule(n_bars=5)]) == "exit after 5 bars"


def test_format_stop_loss():
    assert format_rules_for_prompt([StopLossRule(pct=0.03)]) == "stop loss 3%"


def test_format_stop_loss_trailing_high():
    rule = StopLossRule(pct=0.03, basis="trailing_high")
    assert format_rules_for_prompt([rule]) == "trailing-high stop loss 3%"


def test_format_stop_loss_trailing_low():
    rule = StopLossRule(pct=0.05, basis="trailing_low")
    assert format_rules_for_prompt([rule]) == "trailing-low stop loss 5%"


def test_format_indicator_source_default_omitted():
    rule = EntryRule(
        side="long",
        when=Predicate(lhs=EMARef(period=50), op="gt", rhs=PriceRef(field="close")),
    )
    assert format_rules_for_prompt([rule]) == "long when ema(50) > close"


def test_format_indicator_source_non_default_emitted():
    rule = EntryRule(
        side="long",
        when=Predicate(
            lhs=EMARef(period=50, source="open"),
            op="gt",
            rhs=PriceRef(field="close"),
        ),
    )
    assert format_rules_for_prompt([rule]) == "long when ema(50, source=open) > close"


def test_format_take_profit():
    assert format_rules_for_prompt([TakeProfitRule(pct=0.05)]) == "take profit 5%"


def test_format_signal_exit_rsi_gt_70():
    rule = SignalExitRule(when=Predicate(lhs=RSIRef(period=14), op="gt", rhs=ConstRef(value=70)))
    assert format_rules_for_prompt([rule]) == "exit when rsi(14) > 70"


def test_format_unparsable_returns_prose():
    assert format_rules_for_prompt([UnparsableRule(prose="enter on vibes")]) == "enter on vibes"


def test_format_sizing_fixed_fraction():
    assert format_sizing_rule(FixedFractionSizing(fraction=0.02)) == "risk 2% per trade"


def test_format_sizing_volatility_target():
    assert format_sizing_rule(VolatilityTargetSizing(target_annual_vol=0.10)) == "vol-target 10%"


def test_format_sizing_fixed_notional():
    assert format_sizing_rule(FixedNotionalSizing(notional_usd=50000)) == "$50000 per trade"


def test_format_sizing_unparsable_returns_prose():
    assert (
        format_sizing_rule(UnparsableSizing(prose="size up on confidence"))
        == "size up on confidence"
    )


def test_format_rules_for_prompt_join_separator():
    rules = [
        StopLossRule(pct=0.03),
        TakeProfitRule(pct=0.05),
        TimeStopRule(n_bars=10),
    ]
    assert format_rules_for_prompt(rules) == "stop loss 3%, take profit 5%, exit after 10 bars"


def test_format_rules_for_prompt_empty():
    assert format_rules_for_prompt([]) == ""


# ---------------------------------------------------------------------------
# Issue #551 — StrategySpec wires the DSL types directly. No coercion.
# ---------------------------------------------------------------------------


def _make_structured_strategy_spec():
    from investment_team.models import StrategySpec

    return StrategySpec(
        strategy_id="t",
        authored_by="t",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=SMARef(period=20)),
            ),
        ],
        exit_rules=[TimeStopRule(n_bars=10)],
        sizing=FixedFractionSizing(fraction=0.02),
    )


def test_strategy_spec_round_trips_structured_dsl():
    from investment_team.models import StrategySpec

    spec = _make_structured_strategy_spec()
    assert StrategySpec.model_validate_json(spec.model_dump_json()) == spec


def test_strategy_spec_default_sizing_is_fixed_fraction_two_pct():
    from investment_team.models import StrategySpec

    spec = StrategySpec(
        strategy_id="t",
        authored_by="t",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
    )
    assert isinstance(spec.sizing, FixedFractionSizing)
    assert spec.sizing.fraction == 0.02


def test_strategy_spec_rejects_prose_entry_rules():
    from investment_team.models import StrategySpec

    with pytest.raises(ValidationError):
        StrategySpec(
            strategy_id="t",
            authored_by="t",
            asset_class="stocks",
            hypothesis="h",
            signal_definition="s",
            entry_rules=["close > sma(20)"],
        )


def test_strategy_spec_rejects_prose_exit_rules():
    from investment_team.models import StrategySpec

    with pytest.raises(ValidationError):
        StrategySpec(
            strategy_id="t",
            authored_by="t",
            asset_class="stocks",
            hypothesis="h",
            signal_definition="s",
            exit_rules=["exit after 10 bars"],
        )


def test_strategy_spec_rejects_prose_sizing():
    from investment_team.models import StrategySpec

    with pytest.raises(ValidationError):
        StrategySpec(
            strategy_id="t",
            authored_by="t",
            asset_class="stocks",
            hypothesis="h",
            signal_definition="s",
            sizing="risk 2% per trade",
        )


def test_strategy_spec_has_no_sizing_rules_field():
    """`sizing_rules` was replaced by the singular `sizing` field; extra=forbid
    is not set on StrategySpec, but the field name change is part of the API
    surface and must be observable."""
    from investment_team.models import StrategySpec

    assert "sizing_rules" not in StrategySpec.model_fields
    assert "sizing" in StrategySpec.model_fields
