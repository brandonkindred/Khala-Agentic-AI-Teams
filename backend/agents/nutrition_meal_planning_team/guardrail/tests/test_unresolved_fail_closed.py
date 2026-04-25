"""SPEC-007 §4.4 step 5 — fail-closed unknown-ingredient policy.

- ``canonical_id is None`` and ``confidence < 0.85`` → hard reject.
- ``canonical_id is None`` and ``confidence >= 0.85`` → flag.
- ``canonical_id is None`` and ``confidence == 0.85`` → flag (boundary).
"""

from __future__ import annotations

import pytest
from agents.nutrition_meal_planning_team.guardrail import (
    Severity,
    ViolationReason,
    check_recommendation,
)
from agents.nutrition_meal_planning_team.guardrail import checker as checker_mod
from agents.nutrition_meal_planning_team.ingredient_kb.types import ParsedIngredient

from ._fixtures import profile_with, recipe


def test_unknown_low_confidence_hard_rejects() -> None:
    """A string the parser can't resolve with high confidence must be
    hard-rejected even with no allergens or dietary restrictions."""
    profile = profile_with()
    rec = recipe("xyznotaningredient")

    result = check_recommendation(profile, rec)

    assert result.passed is False
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.reason is ViolationReason.unresolved_ingredient
    assert v.severity is Severity.hard_reject
    assert v.canonical_id is None
    assert v.tag is None


def test_unknown_high_confidence_flags_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """``confidence >= 0.85`` with ``canonical_id=None`` → flag, not reject."""

    def fake_parse(raw: str) -> ParsedIngredient:
        return ParsedIngredient(
            raw=raw,
            qty=None,
            unit=None,
            name=raw,
            modifiers=(),
            canonical_id=None,
            confidence=0.95,
            reasons=("unknown",),
        )

    monkeypatch.setattr(checker_mod, "parse_ingredient", fake_parse)

    profile = profile_with()
    rec = recipe("kohlrabi greens")

    result = check_recommendation(profile, rec)

    assert result.passed is True
    assert result.violations == ()
    assert len(result.flags) == 1
    f = result.flags[0]
    assert f.reason is ViolationReason.unresolved_ingredient
    assert f.severity is Severity.flag


def test_threshold_boundary_is_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec uses strict ``<`` for the hard-reject branch, so exactly
    0.85 is a flag."""

    def fake_parse(raw: str) -> ParsedIngredient:
        return ParsedIngredient(
            raw=raw,
            qty=None,
            unit=None,
            name=raw,
            modifiers=(),
            canonical_id=None,
            confidence=0.85,
            reasons=(),
        )

    monkeypatch.setattr(checker_mod, "parse_ingredient", fake_parse)

    profile = profile_with()
    rec = recipe("borderline ingredient")

    result = check_recommendation(profile, rec)

    assert result.passed is True
    assert len(result.flags) == 1
    assert result.flags[0].severity is Severity.flag


def test_just_below_threshold_is_hard_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_parse(raw: str) -> ParsedIngredient:
        return ParsedIngredient(
            raw=raw,
            qty=None,
            unit=None,
            name=raw,
            modifiers=(),
            canonical_id=None,
            confidence=0.84999,
            reasons=(),
        )

    monkeypatch.setattr(checker_mod, "parse_ingredient", fake_parse)

    profile = profile_with()
    rec = recipe("near miss")

    result = check_recommendation(profile, rec)

    assert result.passed is False
    assert len(result.violations) == 1
    assert result.violations[0].severity is Severity.hard_reject


def test_low_confidence_canonical_match_hard_rejects() -> None:
    """Real-parser case for the broadened fail-closed contract.

    ``parse_ingredient("olive")`` returns a canonical_id (``olive_oil``)
    with confidence 0.5 — the parser fuzzy-matched. Per the
    ``ParsedIngredient`` docstring, ``confidence < 0.85`` is unresolved
    regardless of canonical_id. The checker must hard-reject."""
    profile = profile_with()
    rec = recipe("olive")

    result = check_recommendation(profile, rec)

    assert result.passed is False
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.reason is ViolationReason.unresolved_ingredient
    assert v.severity is Severity.hard_reject


def test_unresolved_does_not_short_circuit_other_ingredients() -> None:
    """An unresolved ingredient yields its own violation but the loop
    still checks remaining ingredients (so we don't miss a real
    allergen later in the list)."""
    from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag

    profile = profile_with(allergens=[AllergenTag.tree_nut])
    rec = recipe("xyznotaningredient", "cashews")

    result = check_recommendation(profile, rec)

    reasons = sorted(v.reason.value for v in result.violations)
    assert reasons == ["allergen", "unresolved_ingredient"]
