"""Public types for the ingredient knowledge base.

Frozen dataclasses so callers can use the values as dict keys / hash
them freely. Any mutation happens at build time in the CLI tools, not
at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .taxonomy import AllergenTag, DietaryTag, InteractionTag


class UnitKind(str, Enum):
    """Coarse unit class used by the converter."""

    mass = "mass"
    volume = "volume"
    count = "count"


@dataclass(frozen=True)
class Unit:
    """One row from ``units.yaml``."""

    name: str  # 'tbsp', 'g', 'count', etc.
    kind: UnitKind
    grams_per_unit: Optional[float] = None
    ml_per_unit: Optional[float] = None


@dataclass(frozen=True)
class PurchaseUnit:
    """Purchase-unit metadata on a canonical food (used by SPEC-014)."""

    unit: str
    typical_package_g: Optional[float] = None
    typical_package_ml: Optional[float] = None


@dataclass(frozen=True)
class CanonicalFood:
    """One row from ``canonical_foods.yaml`` after loading + alias union."""

    id: str
    display_name: str
    allergen_tags: frozenset[AllergenTag] = field(default_factory=frozenset)
    dietary_tags: frozenset[DietaryTag] = field(default_factory=frozenset)
    interaction_tags: frozenset[InteractionTag] = field(default_factory=frozenset)
    aliases: tuple[str, ...] = ()
    fdc_id: Optional[int] = None
    parent_ids: tuple[str, ...] = ()
    purchase_unit: Optional[PurchaseUnit] = None
    aisle_tag: Optional[str] = None  # SPEC-014 §4.2 grocery grouping
    citations: dict = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class ParsedIngredient:
    """Output of ``parse_ingredient``.

    Non-None ``canonical_id`` with ``confidence >= 0.85`` is the
    "resolved" case SPEC-007 passes; anything else is either
    ``unresolved`` (unknown string) or ``ambiguous`` (multiple
    plausible matches). Callers use ``reasons`` to decide behavior.
    """

    raw: str
    qty: Optional[float]
    unit: Optional[Unit]
    name: str
    modifiers: tuple[str, ...]
    canonical_id: Optional[str]
    confidence: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AliasMatch:
    """One hit from the alias index lookup."""

    canonical_id: str
    score: float  # 1.0 for exact, <1.0 for fuzzy
