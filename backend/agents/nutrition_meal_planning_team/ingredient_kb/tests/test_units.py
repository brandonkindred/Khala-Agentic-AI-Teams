"""SPEC-005 §6.4 — unit registry + gram conversions."""

from __future__ import annotations

import pytest

from nutrition_meal_planning_team.ingredient_kb import (
    UnknownUnitError,
    convert_to_grams,
    default_qty_grams,
    get_unit,
    get_units,
    is_known_unit,
)


def test_known_units_cover_v1_set():
    units = get_units()
    required = {"g", "kg", "oz", "lb", "ml", "l", "tsp", "tbsp", "cup", "count"}
    assert required.issubset(units.keys())


def test_unknown_unit_raises():
    with pytest.raises(UnknownUnitError):
        get_unit("cubits")


def test_is_known_unit():
    assert is_known_unit("tbsp") is True
    assert is_known_unit("cubits") is False


# --- mass conversions ----------------------------------------------------


def test_grams_round_trip_identity():
    g = convert_to_grams(qty=100.0, unit=get_unit("g"), canonical_id=None)
    assert g == 100.0


def test_kg_converts_exactly():
    assert convert_to_grams(qty=1.5, unit=get_unit("kg"), canonical_id=None) == 1500.0


def test_ounce_converts():
    g = convert_to_grams(qty=1, unit=get_unit("oz"), canonical_id=None)
    assert g == pytest.approx(28.349523125, abs=1e-6)


def test_pound_converts():
    g = convert_to_grams(qty=1, unit=get_unit("lb"), canonical_id=None)
    assert g == pytest.approx(453.59237, abs=1e-6)


# --- volume: needs density -----------------------------------------------


def test_volume_to_grams_uses_density():
    # 1 tbsp olive oil ≈ 14.7868 ml × 0.913 g/ml ≈ 13.50 g
    g = convert_to_grams(qty=1.0, unit=get_unit("tbsp"), canonical_id="olive_oil")
    assert g is not None
    assert g == pytest.approx(14.7868 * 0.913, abs=1e-3)


def test_volume_without_density_returns_none():
    # canonical_id with no density entry → None (not zero, not fallback).
    g = convert_to_grams(qty=1.0, unit=get_unit("tbsp"), canonical_id="chicken_breast_raw")
    assert g is None


def test_volume_unknown_canonical_returns_none():
    g = convert_to_grams(qty=1.0, unit=get_unit("tbsp"), canonical_id="mystery_food")
    assert g is None


# --- count: needs per-item mass ------------------------------------------


def test_count_to_grams_uses_per_item_mass():
    g = convert_to_grams(qty=2, unit=get_unit("count"), canonical_id="onion_medium")
    assert g == 2 * 170.0


def test_dozen_expands_to_twelve():
    g = convert_to_grams(qty=1, unit=get_unit("dozen"), canonical_id="egg_large")
    assert g == 12 * 50.0


def test_count_without_per_item_returns_none():
    # chicken_breast_raw has no count_to_mass entry.
    g = convert_to_grams(qty=1, unit=get_unit("count"), canonical_id="chicken_breast_raw")
    assert g is None


# --- default qty fallbacks -----------------------------------------------


def test_default_qty_for_handful_items():
    assert default_qty_grams("cashew") == 30.0
    assert default_qty_grams("almond") == 30.0


def test_default_qty_none_for_most_items():
    assert default_qty_grams("chicken_breast_raw") is None
    assert default_qty_grams("olive_oil") is None


def test_default_qty_for_to_taste_salt():
    assert default_qty_grams("salt") == 1.0
