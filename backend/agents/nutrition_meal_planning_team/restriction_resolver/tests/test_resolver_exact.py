"""SPEC-006 §6.1 — exact alias resolution."""

from nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag
from nutrition_meal_planning_team.restriction_resolver import resolve_restrictions


def _only_resolved(result):
    assert result.ambiguous == []
    assert result.unresolved == []
    return result.resolved


def test_cashew_resolves_via_exact_alias():
    r = resolve_restrictions(["cashew"], [])
    resolved = _only_resolved(r)
    assert len(resolved) == 1
    entry = resolved[0]
    assert entry.raw == "cashew"
    assert entry.rule == "exact_alias"
    assert entry.confidence == 1.0
    assert AllergenTag.tree_nut in entry.allergen_tags
    assert "cashew" in entry.matched_canonical_ids


def test_almond_resolves_with_tree_nut():
    r = resolve_restrictions(["almond"], [])
    resolved = _only_resolved(r)
    assert len(resolved) == 1
    assert AllergenTag.tree_nut in resolved[0].allergen_tags
    assert "almond" in resolved[0].matched_canonical_ids


def test_peanut_resolves_with_peanut_allergen():
    r = resolve_restrictions(["peanut"], [])
    resolved = _only_resolved(r)
    assert len(resolved) == 1
    assert AllergenTag.peanut in resolved[0].allergen_tags
    assert "peanut" in resolved[0].matched_canonical_ids


def test_shellfish_category_match():
    # "shellfish" is not a canonical food; it's an allergen category.
    r = resolve_restrictions(["shellfish"], [])
    resolved = _only_resolved(r)
    assert len(resolved) == 1
    entry = resolved[0]
    assert entry.rule == "category"
    assert entry.allergen_tags == [AllergenTag.shellfish]
    assert entry.matched_canonical_ids == []
