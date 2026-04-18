"""SPEC-005 §6.1 — parser fixture suite.

Parametric table-driven tests. Keep the fixtures inline rather than
in a separate YAML so a failure points at the exact case without a
second file read.
"""

from __future__ import annotations

import pytest

from nutrition_meal_planning_team.ingredient_kb import parse_ingredient

# --- Qty + unit + canonical_id ------------------------------------------


@pytest.mark.parametrize(
    "raw,qty,unit_name,canonical_id",
    [
        ("1 tbsp olive oil", 1.0, "tbsp", "olive_oil"),
        ("2 tbsp olive oil", 2.0, "tbsp", "olive_oil"),
        ("1/2 cup rice", 0.5, "cup", "rice_white_raw"),
        ("400 g chicken thighs", 400.0, "g", "chicken_thigh_raw"),
        ("3 onions", 3.0, None, "onion_medium"),
        ("2 cloves garlic", 2.0, None, "garlic_clove"),
        ("1 large onion", 1.0, None, "onion_large"),
    ],
)
def test_parses_qty_unit_canonical(raw, qty, unit_name, canonical_id):
    p = parse_ingredient(raw)
    if qty is not None:
        assert p.qty == pytest.approx(qty)
    if unit_name is not None:
        assert p.unit is not None
        assert p.unit.name == unit_name
    assert p.canonical_id == canonical_id


# --- Unparsed qty paths --------------------------------------------------


def test_handful_of_cashews_resolves_canonical_but_not_qty():
    p = parse_ingredient("a handful of cashews")
    assert p.canonical_id == "cashew"
    assert p.qty is None
    assert "unparsed_qty" in p.reasons


def test_salt_to_taste_resolves():
    p = parse_ingredient("salt, to taste")
    assert p.canonical_id == "salt"
    assert p.qty is None
    assert "unparsed_qty" in p.reasons


def test_pinch_of_cinnamon():
    p = parse_ingredient("a pinch of cinnamon")
    assert p.canonical_id == "cinnamon"


# --- Modifier handling ---------------------------------------------------


def test_diced_modifier_comma_form():
    p = parse_ingredient("400 g chicken thighs, diced")
    assert p.canonical_id == "chicken_thigh_raw"
    assert "diced" in p.modifiers


def test_modifier_leading_word():
    p = parse_ingredient("2 diced tomatoes")
    assert p.canonical_id == "tomato_medium"
    assert "diced" in p.modifiers


# --- Paren size hint -----------------------------------------------------


def test_paren_size_overrides_outer_number():
    """'1 (14 oz) can of chickpeas' → use 14 oz as the purchase qty."""
    p = parse_ingredient("1 (14 oz) can of chickpeas")
    assert p.canonical_id == "chickpeas_cooked"
    assert p.qty == pytest.approx(14.0)
    assert p.unit is not None and p.unit.name == "oz"


# --- Juice of special case ----------------------------------------------


def test_juice_of_one_lemon():
    p = parse_ingredient("juice of 1 lemon")
    assert p.canonical_id == "lemon"
    assert p.qty == 1.0
    assert p.unit is not None and p.unit.name == "count"
    assert "juice" in p.modifiers


def test_juice_of_two_limes():
    p = parse_ingredient("juice of 2 limes")
    assert p.canonical_id == "lime"
    assert p.qty == 2.0


# --- Unresolved / ambiguous ----------------------------------------------


def test_mystery_food_returns_unknown():
    p = parse_ingredient("mysterious greens")
    assert p.canonical_id is None
    assert "unknown" in p.reasons


def test_empty_string_is_safe():
    p = parse_ingredient("")
    assert p.canonical_id is None
    assert "empty_input" in p.reasons


def test_whitespace_only_is_safe():
    p = parse_ingredient("   \t  ")
    assert p.canonical_id is None


# --- Case + pluralization -----------------------------------------------


def test_uppercase_input():
    p = parse_ingredient("OLIVE OIL")
    assert p.canonical_id == "olive_oil"


def test_plural_tomatoes_resolves_singular():
    p = parse_ingredient("tomatoes")
    assert p.canonical_id == "tomato_medium"


def test_plural_potatoes():
    p = parse_ingredient("2 potatoes")
    assert p.canonical_id == "potato"


# --- Confidence ---------------------------------------------------------


def test_confidence_high_on_exact_match():
    p = parse_ingredient("1 tbsp olive oil")
    assert p.confidence >= 0.85


def test_confidence_low_on_unknown():
    p = parse_ingredient("mystery_greens_42")
    assert p.confidence < 0.85


# --- Determinism --------------------------------------------------------


def test_determinism():
    """Same input → byte-equal output, repeatedly."""
    s = "1 tbsp olive oil"
    a = parse_ingredient(s)
    b = parse_ingredient(s)
    assert a == b


# --- Allergen-dense inputs that SPEC-007 will red-team against ----------


def test_soy_sauce_resolves():
    p = parse_ingredient("2 tbsp soy sauce")
    assert p.canonical_id == "soy_sauce"


def test_tamari_is_separate_from_soy_sauce():
    p = parse_ingredient("2 tbsp tamari")
    assert p.canonical_id == "tamari"


def test_worcestershire_resolves():
    p = parse_ingredient("1 tsp worcestershire sauce")
    assert p.canonical_id == "worcestershire"


def test_pine_nuts_resolves_as_tree_nut():
    p = parse_ingredient("1/4 cup pine nuts")
    assert p.canonical_id == "pine_nut"


def test_almond_flour_distinct_from_almond():
    """Flour variant is a different canonical id than the raw nut."""
    p1 = parse_ingredient("1 cup almond flour")
    p2 = parse_ingredient("1 cup almonds")
    assert p1.canonical_id == "almond_flour"
    assert p2.canonical_id == "almond"
    assert p1.canonical_id != p2.canonical_id
