"""Unit tests for the shared prompt-context helpers.

``render_prior_attempts`` and ``spec_prompt_fields`` are pure extractions of
blocks that were duplicated verbatim across the Strategy Lab agents. These
tests lock in both the extracted behaviour and, via characterization tests
that re-derive the original inline formulas independently, that the
extraction changed no rendered byte.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents._prompt_context import (
    render_prior_attempts,
    spec_prompt_fields,
)
from investment_team.strategy_lab.spec_dsl import format_rules_for_prompt, format_sizing_rule


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-prompt-context",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )


def _original_prior_attempts(prior_attempts: Optional[List[str]]) -> str:
    """The exact inline expression `render_prior_attempts` replaced."""
    return (
        "None yet."
        if not prior_attempts
        else "\n".join(f"  Round {i + 1}: {a}" for i, a in enumerate(prior_attempts))
    )


class _UnrecognizedSizing:
    """Neither `FixedFractionSizing`, `VolatilityTargetSizing`, nor
    `FixedNotionalSizing` — `format_sizing_rule` has no branch for it."""


# ---------------------------------------------------------------------------
# render_prior_attempts
# ---------------------------------------------------------------------------


def test_render_prior_attempts_none() -> None:
    assert render_prior_attempts(None) == "None yet."


def test_render_prior_attempts_empty_list() -> None:
    assert render_prior_attempts([]) == "None yet."


def test_render_prior_attempts_single_entry() -> None:
    assert render_prior_attempts(["fixed the guard"]) == "  Round 1: fixed the guard"


def test_render_prior_attempts_multiple_entries_are_one_indexed_in_order() -> None:
    result = render_prior_attempts(["first", "second", "third"])
    assert result == "  Round 1: first\n  Round 2: second\n  Round 3: third"


def test_render_prior_attempts_tolerates_non_string_elements() -> None:
    """Elements are only ever f-string-interpolated, never inspected."""
    result = render_prior_attempts([123, None, {"x": 1}])
    assert result == "  Round 1: 123\n  Round 2: None\n  Round 3: {'x': 1}"


@pytest.mark.parametrize(
    "prior_attempts",
    [None, [], ["one"], ["one", "two"], ["a", "b", "c", "d"]],
)
def test_render_prior_attempts_matches_original_inline_formula(
    prior_attempts: Optional[List[str]],
) -> None:
    """Characterization test: proves this is a byte-preserving extraction."""
    assert render_prior_attempts(prior_attempts) == _original_prior_attempts(prior_attempts)


# ---------------------------------------------------------------------------
# spec_prompt_fields
# ---------------------------------------------------------------------------

_EXPECTED_KEYS = {
    "asset_class",
    "hypothesis",
    "signal_definition",
    "entry_rules",
    "exit_rules",
    "sizing_rules",
    "risk_limits",
}


def test_spec_prompt_fields_returns_exactly_seven_keys() -> None:
    assert set(spec_prompt_fields(_spec())) == _EXPECTED_KEYS


def test_spec_prompt_fields_defensive_false_matches_direct_access_formula() -> None:
    """Characterization test against the original non-defensive call sites'
    exact formula (e.g. `refinement.py`, `zero_trade_repair.py`)."""
    spec = _spec()
    expected = {
        "asset_class": spec.asset_class,
        "hypothesis": spec.hypothesis,
        "signal_definition": spec.signal_definition,
        "entry_rules": format_rules_for_prompt(spec.entry_rules),
        "exit_rules": format_rules_for_prompt(spec.exit_rules),
        "sizing_rules": format_sizing_rule(spec.sizing),
        "risk_limits": spec.risk_limits.model_dump_json(),
    }
    assert spec_prompt_fields(spec, defensive=False) == expected


def test_spec_prompt_fields_defensive_false_raises_on_missing_attribute() -> None:
    """Non-defensive mode adds no tolerance beyond what the original direct
    attribute access had."""
    with pytest.raises(AttributeError):
        spec_prompt_fields(SimpleNamespace(), defensive=False)


def test_spec_prompt_fields_defensive_true_on_none_returns_all_fallbacks() -> None:
    assert spec_prompt_fields(None, defensive=True) == {
        "asset_class": "?",
        "hypothesis": "?",
        "signal_definition": "?",
        "entry_rules": "",
        "exit_rules": "",
        "sizing_rules": "(none)",
        "risk_limits": "",
    }


def test_spec_prompt_fields_defensive_true_on_full_spec_matches_non_defensive() -> None:
    """Both branches converge on well-formed input."""
    spec = _spec()
    assert spec_prompt_fields(spec, defensive=True) == spec_prompt_fields(spec, defensive=False)


def test_spec_prompt_fields_defensive_true_sizing_none_renders_none_marker() -> None:
    fake_spec: Any = SimpleNamespace(sizing=None)
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["sizing_rules"] == "(none)"


def test_spec_prompt_fields_defensive_true_risk_limits_missing_renders_empty_string() -> None:
    """`risk_limits` attribute absent entirely -> the fallback getattr's
    default (`""`) is used."""
    fake_spec: Any = SimpleNamespace()
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["risk_limits"] == ""


def test_spec_prompt_fields_defensive_true_risk_limits_explicit_none_renders_none_string() -> None:
    """`risk_limits` attribute present but explicitly `None` -> the `hasattr`
    probe finds the attribute (returns `None`, not the probe's default), so
    the fallback getattr also finds it (returns `None`, not `""`) ->
    `str(None)`. This asymmetry vs. the "missing entirely" case above is
    inherited verbatim from the original `alignment.py` call site."""
    fake_spec: Any = SimpleNamespace(risk_limits=None)
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["risk_limits"] == "None"


def test_spec_prompt_fields_defensive_true_unrecognized_sizing_still_raises() -> None:
    """Defensive mode tolerates missing/None values, not malformed ones."""
    fake_spec: Any = SimpleNamespace(sizing=_UnrecognizedSizing())
    with pytest.raises(TypeError):
        spec_prompt_fields(fake_spec, defensive=True)


def test_spec_prompt_fields_defensive_true_none_rules_render_empty_string() -> None:
    fake_spec: Any = SimpleNamespace(entry_rules=None, exit_rules=None)
    fields = spec_prompt_fields(fake_spec, defensive=True)
    assert fields["entry_rules"] == ""
    assert fields["exit_rules"] == ""
