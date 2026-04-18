"""SPEC-005 §6.5 — normalizer determinism + pluralization."""

from __future__ import annotations

import pytest

from nutrition_meal_planning_team.ingredient_kb.normalizer import normalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Olive Oil", "olive oil"),
        ("Tomatoes", "tomato"),
        ("POTATOES", "potato"),
        ("Leaves", "leaf"),
        ("Chickpeas", "chickpea"),
        ("Cherries", "cherry"),
        ("Blueberries", "blueberry"),
        ("onions", "onion"),
        ("Garlic Cloves", "garlic clove"),
        ("almonds", "almond"),
        # Protective cases (don't over-depluralize).
        ("grass", "grass"),  # ss-ending
        ("bus", "bus"),  # us-ending
        ("miss", "miss"),  # ss-ending
        ("cucumber", "cucumber"),
    ],
)
def test_normalize_pluralization(raw, expected):
    assert normalize(raw) == expected


def test_idempotent():
    """normalize(normalize(s)) == normalize(s) for every input."""
    inputs = [
        "Olive Oil",
        "POTATOES",
        "5 tomatoes, diced",
        "juice of 1 lemon",
        "Almond Flour",
    ]
    for s in inputs:
        once = normalize(s)
        twice = normalize(once)
        assert once == twice, f"not idempotent: {s!r} -> {once!r} -> {twice!r}"


def test_strips_diacritics():
    assert normalize("jalapeño") == "jalapeno"
    assert normalize("café") == "cafe"


def test_keeps_interior_commas():
    """Parser downstream relies on interior commas for modifier split."""
    assert "," in normalize("chicken, diced")


def test_drops_unit_adjacent_stopwords():
    """'of' / 'the' between content words are dropped."""
    assert normalize("handful of cashews") == "handful cashew"
    assert normalize("a cup of flour") == "a cup flour"
