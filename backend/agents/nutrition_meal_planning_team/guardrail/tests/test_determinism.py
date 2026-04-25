"""SPEC-007 §4.4 contract — same ``(profile, rec)`` produces a
byte-equal ``GuardrailResult`` across 100 iterations.
"""

from __future__ import annotations

from agents.nutrition_meal_planning_team.guardrail import check_recommendation
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import (
    AllergenTag,
    DietaryTag,
)

from ._fixtures import profile_with, recipe


def _complex_inputs():
    profile = profile_with(
        allergens=[
            AllergenTag.tree_nut,
            AllergenTag.dairy,
            AllergenTag.gluten,
            AllergenTag.fish,
        ],
        dietary_forbid=[DietaryTag.honey, DietaryTag.alcohol],
    )
    rec = recipe(
        "1 cup cashews",
        "2 tbsp olive oil",
        "1 cup wheat flour",
        "1/2 cup milk",
        "1 tsp honey",
        "100g salmon",
        "1 onion",
        "xyznotaningredient",
        "salt to taste",
        "1/4 cup red wine",
        name="byte_equal_canary",
    )
    return profile, rec


def test_byte_equal_across_100_iterations() -> None:
    profile, rec = _complex_inputs()
    first = check_recommendation(profile, rec)

    for _ in range(99):
        again = check_recommendation(profile, rec)
        assert again == first
        assert hash(again) == hash(first)


def test_violation_order_is_stable() -> None:
    """Violation tuple order is determined by ingredient position
    then by sorted tag value — deterministic regardless of set
    iteration order in the underlying ``RestrictionResolution``."""
    profile, rec = _complex_inputs()
    first = check_recommendation(profile, rec)

    triples_first = [(v.reason.value, v.ingredient_raw, v.tag) for v in first.violations]
    for _ in range(20):
        result = check_recommendation(profile, rec)
        triples = [(v.reason.value, v.ingredient_raw, v.tag) for v in result.violations]
        assert triples == triples_first


def test_parsed_ingredients_order_matches_recipe() -> None:
    profile, rec = _complex_inputs()
    result = check_recommendation(profile, rec)

    assert tuple(p.raw for p in result.parsed_ingredients) == tuple(rec.ingredients)
