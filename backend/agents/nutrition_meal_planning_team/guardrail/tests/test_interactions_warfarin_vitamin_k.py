"""SPEC-007 §4.4 step 4 — warfarin × vitamin_k_high flag (non-blocking) tests."""

from __future__ import annotations

import pytest
from agents.nutrition_meal_planning_team.guardrail import (
    GuardrailResult,
    Severity,
    ViolationReason,
    check_recommendation,
)

from ._fixtures import profile_with, recipe

WARFARIN_PROFILE = profile_with(medications=["warfarin"])

VITAMIN_K_FOODS = [
    pytest.param("kale", id="kale_raw"),
    pytest.param("spinach", id="spinach_raw"),
]


@pytest.mark.parametrize("ingredient", VITAMIN_K_FOODS)
def test_warfarin_vitamin_k_flags_not_rejects(ingredient: str) -> None:
    result: GuardrailResult = check_recommendation(WARFARIN_PROFILE, recipe(ingredient))

    assert result.passed is True
    assert not any(v.reason is ViolationReason.interaction_hard for v in result.violations)

    interaction_flags = [f for f in result.flags if f.reason is ViolationReason.interaction_flag]
    assert len(interaction_flags) >= 1
    assert any(f.tag == "vitamin_k_high" for f in interaction_flags)
    assert all(f.severity is Severity.flag for f in interaction_flags)


@pytest.mark.parametrize("ingredient", VITAMIN_K_FOODS)
def test_no_medication_no_vitamin_k_flag(ingredient: str) -> None:
    result: GuardrailResult = check_recommendation(profile_with(), recipe(ingredient))

    interaction_flags = [
        f
        for f in result.flags
        if f.reason in (ViolationReason.interaction_hard, ViolationReason.interaction_flag)
    ]
    assert interaction_flags == []


def test_warfarin_vitamin_k_flag_fields() -> None:
    result: GuardrailResult = check_recommendation(WARFARIN_PROFILE, recipe("kale"))

    f = next(f for f in result.flags if f.reason is ViolationReason.interaction_flag)
    assert f.ingredient_raw == "kale"
    assert f.canonical_id == "kale_raw"
    assert f.tag == "vitamin_k_high"
    assert "warfarin" in f.detail
    assert f.severity is Severity.flag


def test_warfarin_safe_food_no_flag() -> None:
    result: GuardrailResult = check_recommendation(WARFARIN_PROFILE, recipe("chicken breast"))

    interaction_flags = [
        f
        for f in result.flags
        if f.reason in (ViolationReason.interaction_hard, ViolationReason.interaction_flag)
    ]
    assert interaction_flags == []
    assert result.passed is True
