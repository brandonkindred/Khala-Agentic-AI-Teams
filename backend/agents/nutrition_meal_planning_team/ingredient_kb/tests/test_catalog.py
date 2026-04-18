"""SPEC-005 §6.3 — catalog lint + alias index invariants."""

from __future__ import annotations

from nutrition_meal_planning_team.ingredient_kb import (
    AllergenTag,
    get_alias_index,
    get_catalog,
)
from nutrition_meal_planning_team.ingredient_kb.taxonomy import FDA_BIG9


def test_catalog_loads_nonzero():
    """v1 seed is ~80-100 ingredients; threshold here is a smoke test
    against accidental file-load regressions, not the aspirational
    2000-entry target from SPEC-005 §4.4 (tracked separately as a
    data-curation backlog). The audit CLI drives that growth."""
    catalog = get_catalog()
    assert len(catalog) >= 70


def test_every_row_has_snake_case_id():
    for food_id in get_catalog():
        assert food_id == food_id.lower(), food_id
        assert all(c.isalnum() or c == "_" for c in food_id), food_id


def test_every_row_has_display_name_in_aliases():
    """SPEC-005 lint: display_name lowercased must appear in aliases."""
    for food in get_catalog().values():
        lower = food.display_name.lower()
        assert lower in {a.lower() for a in food.aliases}, food.id


def test_allergen_rows_have_citation():
    """Any FDA-Big-9 allergen tag requires a non-empty citations.allergen."""
    for food in get_catalog().values():
        fda_tags = food.allergen_tags & FDA_BIG9
        if fda_tags:
            assert food.citations.get("allergen"), (
                f"{food.id} has FDA allergen(s) {fda_tags} but no citation"
            )


def test_soy_sauce_tags_wheat_and_gluten_and_soy():
    """Regression fixture: core allergen classification."""
    food = get_catalog()["soy_sauce"]
    assert AllergenTag.soy in food.allergen_tags
    assert AllergenTag.wheat in food.allergen_tags
    assert AllergenTag.gluten in food.allergen_tags


def test_tamari_tags_soy_not_wheat_not_gluten():
    """tamari is a distinct canonical_id, NOT an alias of soy_sauce."""
    food = get_catalog()["tamari"]
    assert AllergenTag.soy in food.allergen_tags
    assert AllergenTag.wheat not in food.allergen_tags
    assert AllergenTag.gluten not in food.allergen_tags


def test_almond_flour_is_tree_nut():
    food = get_catalog()["almond_flour"]
    assert AllergenTag.tree_nut in food.allergen_tags


def test_worcestershire_tags_fish():
    """Traps pescatarian-except-fish diners."""
    food = get_catalog()["worcestershire"]
    assert AllergenTag.fish in food.allergen_tags


def test_pesto_tags_tree_nut_and_dairy():
    food = get_catalog()["pesto_basil"]
    assert AllergenTag.tree_nut in food.allergen_tags
    assert AllergenTag.dairy in food.allergen_tags


def test_caesar_dressing_tags_fish_egg_dairy():
    food = get_catalog()["caesar_dressing"]
    for tag in (AllergenTag.fish, AllergenTag.egg, AllergenTag.dairy):
        assert tag in food.allergen_tags


def test_alias_index_builds_and_lookups_succeed():
    index = get_alias_index()
    # Spot-check: "olive oil" resolves.
    match = index.lookup("olive oil")
    assert match is not None
    assert match.canonical_id == "olive_oil"
    assert match.score == 1.0


def test_alias_uniqueness():
    """No alias maps to two different canonical_ids — the loader
    would have raised KBError at import if that happened."""
    # Build the index freshly to make sure the invariant survives.
    get_alias_index()  # if ambiguous, raises KBError
