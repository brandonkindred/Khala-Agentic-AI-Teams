"""SPEC-007 W1 smoke test — exercise every acceptance criterion.

Pure-Python; no fixtures, no Postgres, no LLM.
"""

from __future__ import annotations

import dataclasses

import pytest


def test_public_surface_imports() -> None:
    from agents.nutrition_meal_planning_team.guardrail import (  # noqa: F401
        GUARDRAIL_VERSION,
        GuardrailResult,
        Severity,
        Violation,
        ViolationReason,
        check_recommendation,
        is_guardrail_enabled,
    )


def test_guardrail_version() -> None:
    from agents.nutrition_meal_planning_team.guardrail import GUARDRAIL_VERSION

    assert GUARDRAIL_VERSION == "1.0.0"


def test_check_recommendation_is_wired_to_checker() -> None:
    """W2 replaces the W1 stub. Behaviour is covered in detail by the
    other test files in this directory; here we just confirm the
    public name resolves to the real implementation."""
    from agents.nutrition_meal_planning_team.guardrail import (
        check_recommendation,
    )
    from agents.nutrition_meal_planning_team.guardrail.checker import (
        check_recommendation as impl,
    )

    assert check_recommendation is impl


def test_violation_is_frozen() -> None:
    from agents.nutrition_meal_planning_team.guardrail import (
        Severity,
        Violation,
        ViolationReason,
    )

    v = Violation(
        reason=ViolationReason.allergen,
        ingredient_raw="peanut butter",
        canonical_id=None,
        tag="peanut",
        detail="contains peanut",
        severity=Severity.hard_reject,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.detail = "mutated"  # type: ignore[misc]


def test_guardrail_result_is_frozen() -> None:
    from agents.nutrition_meal_planning_team.guardrail import GuardrailResult

    r = GuardrailResult(
        passed=True,
        violations=(),
        flags=(),
        parsed_ingredients=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.passed = False  # type: ignore[misc]


def test_violation_reason_is_str_enum() -> None:
    from agents.nutrition_meal_planning_team.guardrail import ViolationReason

    assert ViolationReason.allergen.value == "allergen"
    assert isinstance(ViolationReason.allergen, str)


def test_feature_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from agents.nutrition_meal_planning_team.guardrail import is_guardrail_enabled

    monkeypatch.delenv("NUTRITION_GUARDRAIL", raising=False)
    assert is_guardrail_enabled() is False

    monkeypatch.setenv("NUTRITION_GUARDRAIL", "1")
    assert is_guardrail_enabled() is True

    monkeypatch.setenv("NUTRITION_GUARDRAIL", "0")
    assert is_guardrail_enabled() is False
