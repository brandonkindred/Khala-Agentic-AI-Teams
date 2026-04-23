"""SPEC-006 §6.1 — unresolved fall-through."""

from nutrition_meal_planning_team.restriction_resolver import resolve_restrictions


def test_junk_input_lands_in_unresolved():
    r = resolve_restrictions(["xyz-something-random"], [])
    assert r.resolved == []
    assert r.ambiguous == []
    assert r.unresolved == ["xyz-something-random"]


def test_empty_and_whitespace_inputs_are_dropped_silently():
    r = resolve_restrictions(["", "   ", "\t"], ["", "  "])
    assert r.resolved == []
    assert r.ambiguous == []
    assert r.unresolved == []


def test_resolver_stamps_kb_version_and_timestamp():
    r = resolve_restrictions(["vegan"], [])
    assert r.kb_version  # non-empty
    assert r.resolved_at  # ISO-ish
    assert "T" in r.resolved_at  # datetime.isoformat()


def test_unicode_diacritic_normalized():
    # "gluten-frée" normalizes (strip accent) to "gluten-free", which
    # is a shorthand synonym.
    r = resolve_restrictions([], ["gluten-frée"])
    assert len(r.resolved) == 1
    assert r.resolved[0].rule == "shorthand"
