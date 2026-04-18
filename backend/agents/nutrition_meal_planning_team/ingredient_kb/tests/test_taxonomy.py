"""SPEC-005 §6.3 — closed taxonomy stability + superset membership."""

from __future__ import annotations

from nutrition_meal_planning_team.ingredient_kb import (
    EU_EXTRAS,
    FDA_BIG9,
    KB_VERSION,
    AllergenTag,
    DietaryTag,
    InteractionTag,
)


def test_kb_version_format():
    assert isinstance(KB_VERSION, str)
    assert KB_VERSION.count(".") == 2


def test_allergen_tag_is_closed():
    """FDA Big-9 all present in the enum."""
    big9_strings = {
        "peanut",
        "tree_nut",
        "dairy",
        "egg",
        "soy",
        "wheat",
        "fish",
        "shellfish",
        "sesame",
    }
    enum_strings = {t.value for t in AllergenTag}
    assert big9_strings.issubset(enum_strings)


def test_gluten_separate_from_wheat():
    """gluten is a distinct tag — spelt / barley / rye need it."""
    assert AllergenTag.gluten != AllergenTag.wheat
    assert AllergenTag.gluten.value == "gluten"


def test_fda_big9_frozenset_exact_membership():
    expected = {
        AllergenTag.peanut,
        AllergenTag.tree_nut,
        AllergenTag.dairy,
        AllergenTag.egg,
        AllergenTag.soy,
        AllergenTag.wheat,
        AllergenTag.fish,
        AllergenTag.shellfish,
        AllergenTag.sesame,
    }
    assert FDA_BIG9 == frozenset(expected)


def test_eu_extras_disjoint_from_fda_big9():
    assert FDA_BIG9.isdisjoint(EU_EXTRAS)


def test_interaction_tag_covers_spec007_enforcement_set():
    """Every tag SPEC-007's interactions.yaml keys on must be defined."""
    required = {
        "vitamin_k_high",
        "tyramine_high",
        "potassium_high",
        "grapefruit",
        "st_johns_wort",
        "very_high_fat",
        "sodium_very_high",
    }
    enum_values = {t.value for t in InteractionTag}
    assert required.issubset(enum_values)


def test_dietary_tag_covers_vegan_expansion():
    """vegan shorthand (SPEC-006) expands to these forbid-tags."""
    required = {"animal", "dairy", "egg", "honey", "gelatin"}
    enum_values = {t.value for t in DietaryTag}
    assert required.issubset(enum_values)
