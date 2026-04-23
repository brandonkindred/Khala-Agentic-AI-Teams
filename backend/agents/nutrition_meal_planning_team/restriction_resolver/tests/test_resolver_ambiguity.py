"""SPEC-006 §6.1 + §6.2 — ambiguous inputs and default-strict tags."""

from nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag
from nutrition_meal_planning_team.restriction_resolver import resolve_restrictions


def _only_ambiguous(result):
    assert result.resolved == []
    assert result.unresolved == []
    assert len(result.ambiguous) >= 1
    return result.ambiguous


def test_nuts_is_ambiguous_and_default_strict_includes_both():
    r = resolve_restrictions(["nuts"], [])
    amb = _only_ambiguous(r)
    assert amb[0].raw == "nuts"
    assert len(amb[0].candidates) >= 3
    # Default-strict: active tags include both peanut AND tree_nut.
    active = r.active_allergen_tags()
    assert AllergenTag.peanut in active
    assert AllergenTag.tree_nut in active


def test_seafood_candidates_include_mollusc():
    r = resolve_restrictions(["seafood"], [])
    amb = _only_ambiguous(r)
    # Strictest candidate (union) includes mollusc.
    active = r.active_allergen_tags()
    assert AllergenTag.fish in active
    assert AllergenTag.shellfish in active
    assert AllergenTag.mollusc in active
    assert "molluscs" in amb[0].question.lower()


def test_low_carb_is_ambiguous():
    r = resolve_restrictions([], ["low-carb"])
    amb = _only_ambiguous(r)
    assert len(amb[0].candidates) == 2
    # Both candidates carry the low_carb soft constraint.
    assert all(c.soft_constraint == "low_carb" for c in amb[0].candidates)


def test_resolving_ambiguous_after_the_fact_does_not_reprompt():
    # Simulate the flow: initial resolve → ambiguous. After the user's
    # chosen candidate is promoted into ``resolved[]`` (done by the API
    # handler), re-running the resolver over the SAME raw list still
    # yields the ambiguity (resolver is stateless); the promotion lives
    # on the stored profile. This test pins the resolver's purity.
    r1 = resolve_restrictions(["nuts"], [])
    r2 = resolve_restrictions(["nuts"], [])
    assert len(r1.ambiguous) == len(r2.ambiguous) == 1
    assert r1.ambiguous[0].raw == r2.ambiguous[0].raw == "nuts"
