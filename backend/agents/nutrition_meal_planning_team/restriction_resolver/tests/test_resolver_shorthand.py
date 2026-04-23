"""SPEC-006 §6.1 — shorthand expansions."""

from nutrition_meal_planning_team.ingredient_kb.taxonomy import (
    AllergenTag,
    DietaryTag,
)
from nutrition_meal_planning_team.restriction_resolver import resolve_restrictions


def _by_raw(result, raw):
    for r in result.resolved:
        if r.raw == raw:
            return r
    raise AssertionError(f"no resolved entry for raw={raw!r}")


def test_vegan_expands_to_five_dietary_forbids():
    r = resolve_restrictions([], ["vegan"])
    entry = _by_raw(r, "vegan")
    assert entry.rule == "shorthand"
    assert entry.source == "shorthand"
    forbid = set(entry.dietary_tags_forbid)
    assert forbid == {
        DietaryTag.animal,
        DietaryTag.dairy,
        DietaryTag.egg,
        DietaryTag.honey,
        DietaryTag.gelatin,
    }


def test_gluten_free_forbids_gluten_and_wheat_allergens():
    r = resolve_restrictions(["gluten-free"], [])
    entry = _by_raw(r, "gluten-free")
    assert entry.rule == "shorthand"
    assert DietaryTag.gluten in entry.dietary_tags_forbid
    assert AllergenTag.gluten in entry.allergen_tags
    assert AllergenTag.wheat in entry.allergen_tags


def test_pescatarian_forbids_animal_only():
    r = resolve_restrictions([], ["pescatarian"])
    entry = _by_raw(r, "pescatarian")
    assert entry.dietary_tags_forbid == [DietaryTag.animal]
    assert "fish, shellfish" in entry.note


def test_paleo_forbids_dairy_grain_legume():
    r = resolve_restrictions([], ["paleo"])
    entry = _by_raw(r, "paleo")
    forbid = set(entry.dietary_tags_forbid)
    assert forbid == {DietaryTag.dairy, DietaryTag.grain, DietaryTag.legume}
