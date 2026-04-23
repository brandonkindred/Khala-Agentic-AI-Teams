"""SPEC-006 §6.1 — negation-pattern handling."""

from nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag
from nutrition_meal_planning_team.restriction_resolver import resolve_restrictions


def _only_resolved(result, n=1):
    assert result.ambiguous == []
    assert result.unresolved == []
    assert len(result.resolved) == n
    return result.resolved


def test_no_cashews_preserves_raw_and_resolves_like_cashew():
    r = resolve_restrictions(["no cashews"], [])
    resolved = _only_resolved(r)
    entry = resolved[0]
    assert entry.raw == "no cashews"  # raw preserved
    assert AllergenTag.tree_nut in entry.allergen_tags
    assert "cashew" in entry.matched_canonical_ids


def test_avoid_gluten_resolves_via_category():
    r = resolve_restrictions(["avoid gluten"], [])
    resolved = _only_resolved(r)
    entry = resolved[0]
    assert entry.raw == "avoid gluten"
    assert entry.rule == "category"
    assert entry.allergen_tags == [AllergenTag.gluten]


def test_unknown_x_free_falls_through_to_unresolved():
    r = resolve_restrictions(["xyznotathing-free"], [])
    assert r.resolved == []
    assert r.ambiguous == []
    assert r.unresolved == ["xyznotathing-free"]


def test_no_dairy_hits_shorthand_dairy_free():
    # "no dairy" → negation strips to "dairy" → allergen category match.
    # (Not the ``dairy_free`` shorthand since that synonym list is
    # ``dairy-free`` / ``lactose-free``, not bare ``dairy``.) The
    # outcome is still the same forbidden allergen.
    r = resolve_restrictions(["no dairy"], [])
    resolved = _only_resolved(r)
    assert AllergenTag.dairy in resolved[0].allergen_tags
