"""Tests for Nutrient enum — closed-set invariants."""

from __future__ import annotations

from nutrition_meal_planning_team.nutrient_data.data.nutrient_enum import Nutrient


class TestNutrientEnum:
    """Invariants: no duplicates, string-valued, stable member set."""

    def test_all_members_are_str(self):
        for member in Nutrient:
            assert isinstance(member.value, str)
            assert member.value == member.name

    def test_no_duplicate_values(self):
        values = [m.value for m in Nutrient]
        assert len(values) == len(set(values))

    def test_minimum_member_count(self):
        assert len(Nutrient) >= 30

    def test_macros_present(self):
        expected_macros = {"kcal", "protein_g", "fat_g", "carbohydrate_g", "fiber_g"}
        actual = {m.value for m in Nutrient}
        assert expected_macros <= actual

    def test_minerals_present(self):
        expected = {"calcium_mg", "iron_mg", "potassium_mg", "sodium_mg", "zinc_mg"}
        actual = {m.value for m in Nutrient}
        assert expected <= actual

    def test_vitamins_present(self):
        expected = {"vitamin_a_mcg", "vitamin_c_mg", "vitamin_d_mcg", "vitamin_b12_mcg"}
        actual = {m.value for m in Nutrient}
        assert expected <= actual

    def test_string_lookup(self):
        assert Nutrient("kcal") is Nutrient.kcal
        assert Nutrient("protein_g") is Nutrient.protein_g

    def test_member_is_usable_as_dict_key(self):
        d = {Nutrient.kcal: 200.0, Nutrient.protein_g: 25.0}
        assert d[Nutrient.kcal] == 200.0
