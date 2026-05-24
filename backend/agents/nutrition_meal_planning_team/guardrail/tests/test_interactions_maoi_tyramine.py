"""SPEC-007 §4.4 step 4 — MAOI × tyramine_high hard-reject tests."""

from __future__ import annotations

import pytest
from agents.nutrition_meal_planning_team.guardrail import (
    GuardrailResult,
    Severity,
    ViolationReason,
    check_recommendation,
)

from ._fixtures import profile_with, recipe

MAOI_PROFILE = profile_with(medications=["maoi"])

TYRAMINE_FOODS = [
    pytest.param("miso paste", id="miso_paste"),
    pytest.param("red wine", id="wine_red"),
]


@pytest.mark.parametrize("ingredient", TYRAMINE_FOODS)
def test_maoi_tyramine_hard_rejects(ingredient: str) -> None:
    result: GuardrailResult = check_recommendation(MAOI_PROFILE, recipe(ingredient))

    assert result.passed is False
    interaction_violations = [
        v for v in result.violations if v.reason is ViolationReason.interaction_hard
    ]
    assert len(interaction_violations) >= 1
    assert all(v.severity is Severity.hard_reject for v in interaction_violations)
    assert any(v.tag == "tyramine_high" for v in interaction_violations)


@pytest.mark.parametrize("ingredient", TYRAMINE_FOODS)
def test_no_medication_no_interaction_violation(ingredient: str) -> None:
    result: GuardrailResult = check_recommendation(profile_with(), recipe(ingredient))

    interaction_violations = [
        v
        for v in result.violations
        if v.reason in (ViolationReason.interaction_hard, ViolationReason.interaction_flag)
    ]
    interaction_flags = [
        f
        for f in result.flags
        if f.reason in (ViolationReason.interaction_hard, ViolationReason.interaction_flag)
    ]
    assert interaction_violations == []
    assert interaction_flags == []


@pytest.mark.parametrize("ingredient", TYRAMINE_FOODS)
def test_different_medication_no_tyramine_rejection(ingredient: str) -> None:
    warfarin_profile = profile_with(medications=["warfarin"])
    result: GuardrailResult = check_recommendation(warfarin_profile, recipe(ingredient))

    interaction_hard = [
        v for v in result.violations if v.reason is ViolationReason.interaction_hard
    ]
    assert interaction_hard == []


def test_maoi_tyramine_violation_fields() -> None:
    result: GuardrailResult = check_recommendation(MAOI_PROFILE, recipe("miso paste"))

    v = next(v for v in result.violations if v.reason is ViolationReason.interaction_hard)
    assert v.ingredient_raw == "miso paste"
    assert v.canonical_id == "miso_paste"
    assert v.tag == "tyramine_high"
    assert "maoi" in v.detail
    assert v.severity is Severity.hard_reject


def test_maoi_multiple_tyramine_ingredients() -> None:
    result: GuardrailResult = check_recommendation(MAOI_PROFILE, recipe("miso paste", "red wine"))

    assert result.passed is False
    tyramine_violations = [
        v
        for v in result.violations
        if v.reason is ViolationReason.interaction_hard and v.tag == "tyramine_high"
    ]
    assert len(tyramine_violations) == 2
