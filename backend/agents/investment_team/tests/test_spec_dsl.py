"""Tests for the spec_dsl module (issue #537 literal schema)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from investment_team.strategy_lab.spec_dsl import (
    DEFAULT_SIZING_PAYLOAD,
    EntryRule,
    EntryRuleAdapter,
    ExitRuleAdapter,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    IndicatorRefAdapter,
    Predicate,
    SignalExitRule,
    SizingRuleAdapter,
    StopLossRule,
    TakeProfitRule,
    TimeStopRule,
    VolatilityTargetSizing,
    format_rules_for_prompt,
    format_sizing_rule,
)

# ---------------------------------------------------------------------------
# Round-trip serialisation per indicator name.
# ---------------------------------------------------------------------------


_INDICATOR_FIXTURES = [
    IndicatorRef(name="sma", params={"period": 20}),
    IndicatorRef(name="ema", params={"period": 50}, source="open"),
    IndicatorRef(name="rsi", params={"period": 14}),
    IndicatorRef(name="macd", params={"fast": 12, "slow": 26, "signal": 9, "output": "signal"}),
    IndicatorRef(name="bollinger", params={"period": 20, "num_std": 2.0, "band": "upper"}),
    IndicatorRef(name="atr", params={"period": 14}),
    IndicatorRef(name="adx", params={"period": 14}),
    IndicatorRef(name="stochastic", params={"k_period": 14, "d_period": 3, "output": "k"}),
    IndicatorRef(name="vwap"),
]


@pytest.mark.parametrize("model", _INDICATOR_FIXTURES)
def test_indicator_round_trip(model):
    dumped = model.model_dump_json()
    rebuilt = IndicatorRefAdapter.validate_json(dumped)
    assert rebuilt == model


def test_entry_rule_round_trip():
    rule = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close",
            op=">",
            rhs=IndicatorRef(name="sma", params={"period": 20}),
        ),
        note="trend-follow",
    )
    rebuilt = EntryRuleAdapter.validate_json(rule.model_dump_json())
    assert rebuilt == rule


@pytest.mark.parametrize(
    "rule",
    [
        TimeStopRule(n_bars=5),
        StopLossRule(pct=0.03),
        StopLossRule(pct=0.03, basis="trailing_high"),
        TakeProfitRule(pct=0.05),
        SignalExitRule(
            when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70.0)
        ),
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
    ],
)
def test_sizing_round_trip(rule):
    rebuilt = SizingRuleAdapter.validate_json(rule.model_dump_json())
    assert rebuilt == rule


# ---------------------------------------------------------------------------
# Discriminator / type-dispatch — raw dicts route to the right concrete class.
# ---------------------------------------------------------------------------


def test_indicator_dispatch():
    ref = IndicatorRefAdapter.validate_python({"name": "rsi", "params": {"period": 14}})
    assert isinstance(ref, IndicatorRef) and ref.name == "rsi"

    ref = IndicatorRefAdapter.validate_python({"name": "sma", "params": {"period": 20}})
    assert isinstance(ref, IndicatorRef) and ref.name == "sma"


def test_entry_discriminator_dispatch():
    payload = {
        "kind": "entry",
        "side": "long",
        "when": {
            "lhs": "bar.close",
            "op": ">",
            "rhs": {"name": "sma", "params": {"period": 20}},
        },
    }
    parsed = EntryRuleAdapter.validate_python(payload)
    assert isinstance(parsed, EntryRule)
    assert isinstance(parsed.when.rhs, IndicatorRef)
    assert parsed.when.rhs.name == "sma"


def test_exit_discriminator_dispatch():
    assert isinstance(
        ExitRuleAdapter.validate_python({"kind": "time_stop", "n_bars": 5}),
        TimeStopRule,
    )
    assert isinstance(
        ExitRuleAdapter.validate_python({"kind": "stop_loss", "pct": 0.03}),
        StopLossRule,
    )


def test_sizing_discriminator_dispatch():
    assert isinstance(
        SizingRuleAdapter.validate_python({"kind": "fixed_fraction", "fraction": 0.02}),
        FixedFractionSizing,
    )


# ---------------------------------------------------------------------------
# Per-indicator bounds & params — registry-backed `_validate_params`.
# ---------------------------------------------------------------------------


def test_sma_period_lower_bound():
    with pytest.raises(ValidationError):
        IndicatorRef(name="sma", params={"period": 1})


def test_sma_period_upper_bound():
    with pytest.raises(ValidationError):
        IndicatorRef(name="sma", params={"period": 401})


def test_rsi_period_upper_bound():
    with pytest.raises(ValidationError):
        IndicatorRef(name="rsi", params={"period": 201})


def test_bollinger_num_std_must_be_positive():
    with pytest.raises(ValidationError):
        IndicatorRef(name="bollinger", params={"period": 20, "num_std": -1})


def test_macd_fast_lower_bound():
    with pytest.raises(ValidationError):
        IndicatorRef(name="macd", params={"fast": 1})


def test_sma_requires_period():
    with pytest.raises(ValidationError):
        IndicatorRef(name="sma", params={})


def test_indicator_rejects_unknown_param():
    with pytest.raises(ValidationError):
        IndicatorRef(name="rsi", params={"period": 14, "foo": 1})


def test_indicator_rejects_source_when_disallowed():
    # ATR / ADX / Stochastic / VWAP do not accept a `source` override.
    with pytest.raises(ValidationError):
        IndicatorRef(name="atr", params={"period": 14}, source="open")


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
            lhs="bar.close",
            op="bogus",  # type: ignore[arg-type]
            rhs=IndicatorRef(name="sma", params={"period": 20}),
        )


def test_predicate_legacy_op_rejected():
    # Issue #537: ops are symbol literals (">", "<", …) — the legacy
    # "gt"/"lt" name aliases are no longer accepted on direct construction.
    with pytest.raises(ValidationError):
        Predicate(
            lhs="bar.close",
            op="gt",  # type: ignore[arg-type]
            rhs=IndicatorRef(name="sma", params={"period": 20}),
        )


def test_predicate_lhs_unknown_literal_rejected():
    with pytest.raises(ValidationError):
        Predicate(
            lhs="bar.spread",  # type: ignore[arg-type]
            op=">",
            rhs=IndicatorRef(name="sma", params={"period": 20}),
        )


def test_predicate_rhs_accepts_float():
    p = Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0)
    assert p.rhs == 30.0


def test_time_stop_must_be_positive():
    with pytest.raises(ValidationError):
        TimeStopRule(n_bars=0)


def test_fixed_fraction_upper_bound():
    with pytest.raises(ValidationError):
        FixedFractionSizing(fraction=1.5)


def test_extra_field_is_forbidden():
    with pytest.raises(ValidationError):
        IndicatorRef.model_validate({"name": "rsi", "params": {"period": 14}, "foo": 1})


# ---------------------------------------------------------------------------
# Formatter golden strings.
# ---------------------------------------------------------------------------


def test_format_entry_close_gt_sma():
    rule = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
        ),
    )
    assert format_rules_for_prompt([rule]) == "long when close > sma(20)"


def test_format_entry_rsi_lt_30():
    rule = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
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
        when=Predicate(
            lhs=IndicatorRef(name="ema", params={"period": 50}), op=">", rhs="bar.close"
        ),
    )
    assert format_rules_for_prompt([rule]) == "long when ema(50) > close"


def test_format_indicator_source_non_default_emitted():
    rule = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="ema", params={"period": 50}, source="open"),
            op=">",
            rhs="bar.close",
        ),
    )
    assert format_rules_for_prompt([rule]) == "long when ema(50, source=open) > close"


def test_format_take_profit():
    assert format_rules_for_prompt([TakeProfitRule(pct=0.05)]) == "take profit 5%"


def test_format_signal_exit_rsi_gt_70():
    rule = SignalExitRule(
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70.0)
    )
    assert format_rules_for_prompt([rule]) == "exit when rsi(14) > 70"


def test_format_sizing_fixed_fraction():
    assert format_sizing_rule(FixedFractionSizing(fraction=0.02)) == "risk 2% per trade"


def test_format_sizing_volatility_target():
    assert format_sizing_rule(VolatilityTargetSizing(target_annual_vol=0.10)) == "vol-target 10%"


def test_format_sizing_fixed_notional():
    assert format_sizing_rule(FixedNotionalSizing(notional_usd=50000)) == "$50000 per trade"


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
# Issue #537 — `StrategySpec` wires the DSL types and requires `timeframe`.
# ---------------------------------------------------------------------------


def _make_structured_strategy_spec():
    from investment_team.models import StrategySpec

    return StrategySpec(
        strategy_id="t",
        authored_by="t",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
                ),
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
        timeframe="1d",
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
            timeframe="1d",
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
            timeframe="1d",
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
            timeframe="1d",
            sizing="risk 2% per trade",
        )


def test_strategy_spec_has_no_sizing_rules_field():
    from investment_team.models import StrategySpec

    assert "sizing_rules" not in StrategySpec.model_fields
    assert "sizing" in StrategySpec.model_fields


# ---------------------------------------------------------------------------
# Issue #537 — timeframe required, legacy payload migration.
# ---------------------------------------------------------------------------


def test_strategy_spec_timeframe_required():
    from investment_team.models import StrategySpec

    with pytest.raises(ValidationError) as exc_info:
        StrategySpec(
            strategy_id="t",
            authored_by="t",
            asset_class="stocks",
            hypothesis="h",
            signal_definition="s",
        )
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("timeframe",) for e in errors)


@pytest.mark.parametrize("tf", ["1m", "5m", "15m", "1h", "1d"])
def test_strategy_spec_accepts_each_timeframe_literal(tf):
    from investment_team.models import StrategySpec

    spec = StrategySpec(
        strategy_id="t",
        authored_by="t",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe=tf,
    )
    assert spec.timeframe == tf


def test_strategy_spec_rejects_unknown_timeframe():
    from investment_team.models import StrategySpec

    with pytest.raises(ValidationError):
        StrategySpec(
            strategy_id="t",
            authored_by="t",
            asset_class="stocks",
            hypothesis="h",
            signal_definition="s",
            timeframe="7d",  # type: ignore[arg-type]
        )


def test_default_sizing_payload_is_two_pct():
    assert DEFAULT_SIZING_PAYLOAD == {"kind": "fixed_fraction", "fraction": 0.02}
