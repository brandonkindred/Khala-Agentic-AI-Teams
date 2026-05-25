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
