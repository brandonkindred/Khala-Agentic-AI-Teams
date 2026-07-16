"""Additional coverage for ``strategy_lab.spec_dsl``.

Targets the format helpers (``_format_indicator_ref``,
``_format_predicate``, ``_format_rule``, ``format_rules_for_prompt``,
``format_sizing_rule``), the indicator-param validators, and the
predicate-side validators.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    VolatilityTargetSizing,
    _float_gt,
    _format_indicator_ref,
    _format_number,
    _format_predicate,
    _format_rule,
    _format_side,
    _int_in,
    _one_of,
    format_rules_for_prompt,
    format_sizing_rule,
)

# ---------------------------------------------------------------------------
# _int_in / _float_gt / _one_of validators
# ---------------------------------------------------------------------------


def test_int_in_rejects_non_int_and_bool() -> None:
    check = _int_in(1, 10)
    with pytest.raises(ValueError):
        check("five")
    with pytest.raises(ValueError):
        check(True)  # booleans are int subclass — must still reject
    check(5)


def test_int_in_rejects_out_of_range() -> None:
    check = _int_in(2, 4)
    with pytest.raises(ValueError):
        check(1)
    with pytest.raises(ValueError):
        check(5)


def test_float_gt_rejects_non_numeric() -> None:
    check = _float_gt(0.0)
    with pytest.raises(ValueError):
        check("x")
    with pytest.raises(ValueError):
        check(True)
    with pytest.raises(ValueError):
        check(float("nan"))
    check(0.5)


def test_float_gt_rejects_below_threshold() -> None:
    check = _float_gt(1.0)
    with pytest.raises(ValueError):
        check(0.5)
    check(1.5)


def test_one_of_rejects_unlisted() -> None:
    check = _one_of("a", "b")
    with pytest.raises(ValueError):
        check("c")
    check("a")


# ---------------------------------------------------------------------------
# IndicatorRef validation
# ---------------------------------------------------------------------------


def test_indicator_ref_missing_required_param_raises() -> None:
    with pytest.raises(ValueError):
        IndicatorRef(name="sma")  # period is required


def test_indicator_ref_unexpected_param_raises() -> None:
    with pytest.raises(ValueError):
        IndicatorRef(name="rsi", params={"unknown_key": 1})


def test_indicator_ref_source_rejected_for_disallowed() -> None:
    with pytest.raises(ValueError):
        IndicatorRef(name="atr", source="high")  # type: ignore[arg-type]


def test_indicator_ref_fills_optional_defaults() -> None:
    ref = IndicatorRef(name="rsi")
    assert ref.param("period") == 14


# ---------------------------------------------------------------------------
# _format_number
# ---------------------------------------------------------------------------


def test_format_number_renders_integers_as_int() -> None:
    assert _format_number(20.0) == "20"
    assert _format_number(-5.0) == "-5"


def test_format_number_renders_floats_with_repr() -> None:
    out = _format_number(0.05)
    assert out in {"0.05", str(0.05)}


def test_format_number_avoids_scientific_notation_for_small_floats() -> None:
    # repr(1e-05) == "1e-05"; the adapter's decimal-only regex (\d+(?:\.\d+)?)
    # can't parse scientific notation, so the fallback must never emit one.
    for value in (1e-5, -1e-5, 1.5e-8):
        out = _format_number(value)
        assert "e" not in out.lower()
        assert float(out) == value
        assert re.fullmatch(r"-?\d+(?:\.\d+)?", out)


def test_format_number_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        _format_number(float("nan"))
    with pytest.raises(ValueError):
        _format_number(float("inf"))


# ---------------------------------------------------------------------------
# _format_indicator_ref
# ---------------------------------------------------------------------------


def test_format_indicator_ref_handles_all_indicator_families() -> None:
    assert _format_indicator_ref(IndicatorRef(name="sma", params={"period": 20})) == "sma(20)"
    assert _format_indicator_ref(IndicatorRef(name="ema", params={"period": 50})) == "ema(50)"
    assert _format_indicator_ref(IndicatorRef(name="rsi")) == "rsi(14)"
    assert _format_indicator_ref(IndicatorRef(name="macd")) == "macd(12,26,9)"
    assert _format_indicator_ref(IndicatorRef(name="macd", params={"output": "signal"})) == "macd_signal(12,26,9)"
    bb_mid = _format_indicator_ref(IndicatorRef(name="bollinger"))
    assert bb_mid == "bollinger_middle(20,2)"
    assert _format_indicator_ref(IndicatorRef(name="atr")) == "atr(14)"
    assert _format_indicator_ref(IndicatorRef(name="adx")) == "adx(14)"
    assert _format_indicator_ref(IndicatorRef(name="stochastic")) == "stochastic_k(14,3)"
    assert _format_indicator_ref(IndicatorRef(name="vwap")) == "vwap(20)"


def test_format_indicator_ref_with_alt_source_appends_modifier() -> None:
    out = _format_indicator_ref(IndicatorRef(name="sma", params={"period": 20}, source="high"))
    assert "source=high" in out


def test_format_indicator_ref_rsi_with_alt_source_branch() -> None:
    """Same comma-append branch of ``_with_source`` as the sma case above, for rsi."""
    ref = IndicatorRef(name="rsi", source="high")
    out = _format_indicator_ref(ref)
    assert "source=high" in out


def test_format_indicator_ref_unknown_name_raises() -> None:
    # Construct a refcontaining an unknown name via model_construct (bypass validator).
    bad = IndicatorRef.model_construct(name="bogus", params={}, source="close")
    with pytest.raises(TypeError):
        _format_indicator_ref(bad)


# ---------------------------------------------------------------------------
# _format_side / _format_predicate / _format_rule
# ---------------------------------------------------------------------------


def test_format_side_handles_indicator_string_and_number() -> None:
    assert _format_side(IndicatorRef(name="sma", params={"period": 20})) == "sma(20)"
    assert _format_side("bar.close") == "close"
    assert _format_side(100) == "100"
    assert _format_side(100.5).startswith("100.5") or "100.5" in _format_side(100.5)


def test_format_side_rejects_bool_and_unknown_types() -> None:
    with pytest.raises(TypeError):
        _format_side(True)
    with pytest.raises(TypeError):
        _format_side(object())  # type: ignore[arg-type]


def test_format_side_rejects_unexpected_string() -> None:
    with pytest.raises(ValueError):
        _format_side("invalid.field")


def test_format_predicate_renders_op_symbol() -> None:
    pred = Predicate(lhs="bar.close", op=">", rhs=100.0)
    assert _format_predicate(pred) == "close > 100"


def test_format_predicate_cross_above() -> None:
    pred = Predicate(
        lhs=IndicatorRef(name="sma", params={"period": 5}),
        op="cross_above",
        rhs=IndicatorRef(name="sma", params={"period": 20}),
    )
    assert "crosses above" in _format_predicate(pred)


def test_format_rule_entry_stop_take_signal_exit() -> None:
    entry = EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=100.0))
    assert _format_rule(entry) == "long when close > 100"

    stop = StopLossRule(pct=0.05, basis="entry_price")
    assert _format_rule(stop) == "stop loss 5%"

    trailing = StopLossRule(pct=0.02, basis="trailing_high")
    assert _format_rule(trailing) == "trailing-high stop loss 2%"

    tp = TakeProfitRule(pct=0.1)
    assert _format_rule(tp) == "take profit 10%"

    exit_rule = SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=90.0))
    assert _format_rule(exit_rule) == "exit when close < 90"


def test_format_rule_unknown_raises() -> None:
    with pytest.raises(TypeError):
        _format_rule(object())  # type: ignore[arg-type]


def test_format_rules_for_prompt_joins_rules() -> None:
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=100.0)),
        StopLossRule(pct=0.05),
    ]
    out = format_rules_for_prompt(rules, separator=" | ")
    assert out == "long when close > 100 | stop loss 5%"


# ---------------------------------------------------------------------------
# format_sizing_rule
# ---------------------------------------------------------------------------


def test_format_sizing_rule_variants() -> None:
    assert format_sizing_rule(FixedFractionSizing(fraction=0.05)) == "risk 5% per trade"
    assert format_sizing_rule(VolatilityTargetSizing(target_annual_vol=0.12)) == "vol-target 12%"
    assert format_sizing_rule(FixedNotionalSizing(notional_usd=1000.0)) == "$1000 per trade"


def test_format_sizing_rule_unknown_raises() -> None:
    with pytest.raises(TypeError):
        format_sizing_rule(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Predicate validators
# ---------------------------------------------------------------------------


def test_predicate_rejects_unknown_lhs_string() -> None:
    with pytest.raises(ValidationError):
        Predicate(lhs="bar.foo", op=">", rhs=1.0)  # type: ignore[arg-type]


def test_predicate_rejects_unknown_rhs_string() -> None:
    with pytest.raises(ValidationError):
        Predicate(lhs="bar.close", op=">", rhs="bar.foo")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Finite-value validator on _SpecNode.__post_init_validator
# ---------------------------------------------------------------------------


def test_stoploss_rejects_invalid_pct() -> None:
    with pytest.raises(ValidationError):
        StopLossRule(pct=2.0)  # > 1.0 invalid
    with pytest.raises(ValidationError):
        StopLossRule(pct=0.0)  # not > 0


def test_takeprofit_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        TakeProfitRule(pct=0.0)


def test_sizing_rule_rejects_invalid_fraction() -> None:
    with pytest.raises(ValidationError):
        FixedFractionSizing(fraction=0.0)
    with pytest.raises(ValidationError):
        FixedFractionSizing(fraction=1.5)
