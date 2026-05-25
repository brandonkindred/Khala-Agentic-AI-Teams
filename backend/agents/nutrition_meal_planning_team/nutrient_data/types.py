"""Public types for the nutrient data module.

Frozen dataclasses for immutable runtime values. All fields use
SI-consistent units indicated by the nutrient enum member's suffix
(e.g. ``protein_g`` → grams per 100 g edible portion).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Optional

from .data.nutrient_enum import Nutrient


@dataclass(frozen=True)
class NutrientRow:
    """One nutrient value for a single food.

    Preconditions:
        - canonical_id is a valid key in the ingredient KB catalog.
        - nutrient is a member of the Nutrient enum.
        - value_per_100g >= 0.

    Postconditions:
        - Instances are immutable and hashable.
    """

    canonical_id: str
    nutrient: Nutrient
    value_per_100g: float
    data_version: str
    source: str = "fdc"
    is_override: bool = False


@dataclass(frozen=True)
class Nutrients:
    """Full nutrient profile for one food — read-only lookup container.

    Maps each Nutrient enum member to its per-100g value. Missing
    nutrients default to None (not measured / not in FDC for this food).

    Invariants:
        - ``values`` is wrapped in MappingProxyType at construction;
          mutation attempts raise TypeError.
        - Not hashable (use canonical_id + data_version as cache keys).
    """

    canonical_id: str
    data_version: str
    values: MappingProxyType[Nutrient, float] = field(default_factory=lambda: MappingProxyType({}))

    def __init__(
        self,
        canonical_id: str,
        data_version: str,
        values: dict[Nutrient, float] | MappingProxyType[Nutrient, float] | None = None,
    ):
        object.__setattr__(self, "canonical_id", canonical_id)
        object.__setattr__(self, "data_version", data_version)
        proxy = (
            MappingProxyType(values)
            if isinstance(values, dict)
            else (values if values is not None else MappingProxyType({}))
        )
        object.__setattr__(self, "values", proxy)

    def get(self, nutrient: Nutrient) -> Optional[float]:
        """Return value for a nutrient, or None if not available."""
        return self.values.get(nutrient)


@dataclass(frozen=True)
class DensityRecord:
    """Density conversion for a food+unit pair (g per unit volume/count)."""

    canonical_id: str
    unit: str
    grams_per_unit: float
    data_version: str


@dataclass(frozen=True)
class RetentionFactors:
    """Cooking-method retention factors for a food.

    Preconditions:
        - 0 < nutrient_retention <= 1.0 (fraction retained after cooking).
        - mass_retention > 0 (can exceed 1.0 for water-absorbing foods).

    Postconditions:
        - is_default=True when the factors are identity (1.0, 1.0)
          because no specific data exists for this food+method combo.
    """

    canonical_id: str
    method: str
    nutrient_retention: float
    mass_retention: float
    data_version: str
    is_default: bool = False


# Sentinel instance: identity factors (no cooking adjustment)
IDENTITY_RETENTION = RetentionFactors(
    canonical_id="",
    method="raw",
    nutrient_retention=1.0,
    mass_retention=1.0,
    data_version="",
    is_default=True,
)
