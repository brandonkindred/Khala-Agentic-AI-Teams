"""SPEC-007 §4.4 step 2 — allergen cross-matrix.

Every ``AllergenTag`` member must trigger a hard reject when the
profile has that tag active and the recipe contains an exemplar
ingredient.
"""

from __future__ import annotations

import pytest
from agents.nutrition_meal_planning_team.guardrail import (
    Severity,
    ViolationReason,
    check_recommendation,
)
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag

from ._fixtures import profile_with, recipe

# One known-good catalog exemplar per allergen tag. Keep these
# strings simple so the parser hits exact-alias (confidence 1.0).
ALLERGEN_EXEMPLAR: dict[AllergenTag, str] = {
    AllergenTag.peanut: "peanut butter",
    AllergenTag.tree_nut: "cashews",
    AllergenTag.dairy: "milk",
    AllergenTag.egg: "egg",
    AllergenTag.soy: "tofu",
    AllergenTag.wheat: "all-purpose flour",
    AllergenTag.gluten: "pasta",
    AllergenTag.fish: "salmon",
    AllergenTag.shellfish: "shrimp",
    AllergenTag.sesame: "sesame oil",
    AllergenTag.mustard: "mustard",
    AllergenTag.celery: "celery",
    AllergenTag.sulfites: "red wine",
    AllergenTag.lupin: "lupin flour",
    AllergenTag.mollusc: "mussels",
}


def test_every_allergen_tag_has_an_exemplar() -> None:
    """Lock the cross-matrix to the full enum so adding a new tag
    forces the test author to seed the catalog."""
    assert set(ALLERGEN_EXEMPLAR.keys()) == set(AllergenTag)


@pytest.mark.parametrize("tag", list(AllergenTag), ids=lambda t: t.value)
def test_active_allergen_triggers_hard_reject(tag: AllergenTag) -> None:
    profile = profile_with(allergens=[tag])
    rec = recipe(ALLERGEN_EXEMPLAR[tag])

    result = check_recommendation(profile, rec)

    assert result.passed is False
    allergen_violations = [v for v in result.violations if v.reason is ViolationReason.allergen]
    assert allergen_violations, f"no allergen violation for {tag.value}"
    assert any(v.tag == tag.value for v in allergen_violations)
    assert all(v.severity is Severity.hard_reject for v in allergen_violations)


def test_inactive_allergen_does_not_trigger() -> None:
    """A peanut-allergic profile should NOT trip on a tree-nut recipe."""
    profile = profile_with(allergens=[AllergenTag.peanut])
    rec = recipe("cashews")

    result = check_recommendation(profile, rec)

    assert result.passed is True
    assert result.violations == ()


def test_multiple_allergens_in_one_food_each_emit_violation() -> None:
    """Soy sauce carries soy + wheat + gluten. A profile active on
    all three should produce three distinct allergen violations."""
    profile = profile_with(
        allergens=[AllergenTag.soy, AllergenTag.wheat, AllergenTag.gluten],
    )
    rec = recipe("soy sauce")

    result = check_recommendation(profile, rec)

    tags = sorted(v.tag for v in result.violations)
    assert tags == ["gluten", "soy", "wheat"]
