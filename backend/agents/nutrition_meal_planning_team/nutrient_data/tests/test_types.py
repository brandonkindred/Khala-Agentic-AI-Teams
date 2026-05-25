"""Tests for nutrient_data types — frozen dataclass invariants."""

from __future__ import annotations

import pytest

from nutrition_meal_planning_team.nutrient_data.data.nutrient_enum import Nutrient
from nutrition_meal_planning_team.nutrient_data.types import (
    IDENTITY_RETENTION,
    DensityRecord,
    NutrientRow,
    Nutrients,
    RetentionFactors,
)


class TestNutrientRow:
    def test_frozen(self):
        row = NutrientRow(
            canonical_id="chicken_breast",
            nutrient=Nutrient.protein_g,
            value_per_100g=31.0,
            data_version="1.0.0",
        )
        with pytest.raises(AttributeError):
            row.value_per_100g = 99.0  # type: ignore[misc]

    def test_defaults(self):
        row = NutrientRow(
            canonical_id="rice_white",
            nutrient=Nutrient.carbohydrate_g,
            value_per_100g=28.0,
            data_version="1.0.0",
        )
        assert row.source == "fdc"
        assert row.is_override is False

    def test_hashable(self):
        row = NutrientRow(
            canonical_id="egg",
            nutrient=Nutrient.fat_g,
            value_per_100g=11.0,
            data_version="1.0.0",
        )
        assert hash(row) == hash(row)
        s = {row}
        assert row in s

    def test_rejects_negative_value(self):
        with pytest.raises(ValueError, match="value_per_100g must be finite and >= 0"):
            NutrientRow(
                canonical_id="x",
                nutrient=Nutrient.kcal,
                value_per_100g=-1.0,
                data_version="1.0.0",
            )

    def test_accepts_zero_value(self):
        row = NutrientRow(
            canonical_id="x",
            nutrient=Nutrient.alcohol_g,
            value_per_100g=0.0,
            data_version="1.0.0",
        )
        assert row.value_per_100g == 0.0

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_value(self, bad_value: float):
        with pytest.raises(ValueError, match="finite"):
            NutrientRow(
                canonical_id="x",
                nutrient=Nutrient.kcal,
                value_per_100g=bad_value,
                data_version="1.0.0",
            )


class TestNutrients:
    def test_get_existing(self):
        n = Nutrients(
            canonical_id="salmon",
            data_version="1.0.0",
            values={Nutrient.kcal: 208.0, Nutrient.protein_g: 20.0},
        )
        assert n.get(Nutrient.kcal) == 208.0
        assert n.get(Nutrient.protein_g) == 20.0

    def test_get_missing_returns_none(self):
        n = Nutrients(canonical_id="salmon", data_version="1.0.0", values={})
        assert n.get(Nutrient.iron_mg) is None

    def test_frozen_field_reassignment(self):
        n = Nutrients(canonical_id="x", data_version="1.0.0")
        with pytest.raises(AttributeError):
            n.canonical_id = "y"  # type: ignore[misc]

    def test_values_immutable(self):
        n = Nutrients(
            canonical_id="x",
            data_version="1.0.0",
            values={Nutrient.kcal: 100.0},
        )
        with pytest.raises(TypeError):
            n.values[Nutrient.protein_g] = 50.0  # type: ignore[index]

    def test_default_empty_values(self):
        n = Nutrients(canonical_id="x", data_version="1.0.0")
        assert len(n.values) == 0
        assert n.get(Nutrient.kcal) is None

    def test_defensive_copy_of_input_dict(self):
        source = {Nutrient.kcal: 100.0}
        n = Nutrients(canonical_id="x", data_version="1.0.0", values=source)
        source[Nutrient.protein_g] = 25.0
        assert n.get(Nutrient.protein_g) is None
        assert len(n.values) == 1


class TestDensityRecord:
    def test_construction(self):
        d = DensityRecord(
            canonical_id="olive_oil",
            unit="tbsp",
            grams_per_unit=13.5,
            data_version="1.0.0",
        )
        assert d.grams_per_unit == 13.5

    def test_frozen(self):
        d = DensityRecord(
            canonical_id="olive_oil",
            unit="tbsp",
            grams_per_unit=13.5,
            data_version="1.0.0",
        )
        with pytest.raises(AttributeError):
            d.grams_per_unit = 99.0  # type: ignore[misc]

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="grams_per_unit"):
            DensityRecord(canonical_id="x", unit="cup", grams_per_unit=0.0, data_version="1.0.0")

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="grams_per_unit"):
            DensityRecord(canonical_id="x", unit="cup", grams_per_unit=-5.0, data_version="1.0.0")

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
    def test_rejects_non_finite(self, bad_value: float):
        with pytest.raises(ValueError, match="finite"):
            DensityRecord(
                canonical_id="x", unit="cup", grams_per_unit=bad_value, data_version="1.0.0"
            )


class TestRetentionFactors:
    def test_construction(self):
        r = RetentionFactors(
            canonical_id="chicken_breast",
            method="grilled",
            nutrient_retention=0.85,
            mass_retention=0.75,
            data_version="1.0.0",
        )
        assert r.nutrient_retention == 0.85
        assert r.mass_retention == 0.75
        assert r.is_default is False

    def test_identity_sentinel(self):
        assert IDENTITY_RETENTION.nutrient_retention == 1.0
        assert IDENTITY_RETENTION.mass_retention == 1.0
        assert IDENTITY_RETENTION.is_default is True

    def test_frozen(self):
        r = RetentionFactors(
            canonical_id="x",
            method="raw",
            nutrient_retention=1.0,
            mass_retention=1.0,
            data_version="1.0.0",
        )
        with pytest.raises(AttributeError):
            r.method = "boiled"  # type: ignore[misc]

    def test_rejects_zero_nutrient_retention(self):
        with pytest.raises(ValueError, match="nutrient_retention"):
            RetentionFactors(
                canonical_id="x",
                method="boiled",
                nutrient_retention=0.0,
                mass_retention=0.8,
                data_version="1.0.0",
            )

    def test_rejects_nutrient_retention_above_one(self):
        with pytest.raises(ValueError, match="nutrient_retention"):
            RetentionFactors(
                canonical_id="x",
                method="boiled",
                nutrient_retention=1.1,
                mass_retention=0.8,
                data_version="1.0.0",
            )

    def test_rejects_zero_mass_retention(self):
        with pytest.raises(ValueError, match="mass_retention"):
            RetentionFactors(
                canonical_id="x",
                method="boiled",
                nutrient_retention=0.9,
                mass_retention=0.0,
                data_version="1.0.0",
            )

    def test_allows_mass_retention_above_one(self):
        r = RetentionFactors(
            canonical_id="pasta",
            method="boiled",
            nutrient_retention=0.95,
            mass_retention=2.0,
            data_version="1.0.0",
        )
        assert r.mass_retention == 2.0

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
    def test_rejects_non_finite_nutrient_retention(self, bad_value: float):
        with pytest.raises(ValueError, match="finite"):
            RetentionFactors(
                canonical_id="x",
                method="boiled",
                nutrient_retention=bad_value,
                mass_retention=0.8,
                data_version="1.0.0",
            )

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
    def test_rejects_non_finite_mass_retention(self, bad_value: float):
        with pytest.raises(ValueError, match="finite"):
            RetentionFactors(
                canonical_id="x",
                method="boiled",
                nutrient_retention=0.9,
                mass_retention=bad_value,
                data_version="1.0.0",
            )
