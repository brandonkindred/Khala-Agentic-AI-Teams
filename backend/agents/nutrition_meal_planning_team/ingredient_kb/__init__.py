"""Ingredient knowledge base (SPEC-005).

Public entry points:

    from nutrition_meal_planning_team.ingredient_kb import (
        KB_VERSION,
        parse_ingredient,
        AllergenTag, DietaryTag, InteractionTag,
        CanonicalFood, ParsedIngredient, Unit,
        get_catalog, get_alias_index,
        convert_to_grams, default_qty_grams, get_unit, is_known_unit,
        KBError, UnknownUnitError,
    )

``parse_ingredient`` is the entry point downstream specs (SPEC-006,
SPEC-007, SPEC-009) use. Direct catalog / alias-index access is for
CLI tools, tests, and the nutrient-data ingestion pipeline (SPEC-008).
"""

from __future__ import annotations

from .catalog import AliasIndex, get_alias_index, get_catalog, get_densities
from .errors import AmbiguousIngredientError, KBError, UnknownUnitError
from .parser import parse_ingredient
from .taxonomy import (
    EU_EXTRAS,
    FDA_BIG9,
    AllergenTag,
    DietaryTag,
    InteractionTag,
)
from .types import (
    AliasMatch,
    CanonicalFood,
    ParsedIngredient,
    PurchaseUnit,
    Unit,
    UnitKind,
)
from .units import (
    convert_to_grams,
    default_qty_grams,
    get_unit,
    get_units,
    is_known_unit,
)
from .version import KB_VERSION

__all__ = [
    # Version
    "KB_VERSION",
    # Taxonomy enums
    "AllergenTag",
    "DietaryTag",
    "InteractionTag",
    "FDA_BIG9",
    "EU_EXTRAS",
    # Types
    "AliasMatch",
    "CanonicalFood",
    "ParsedIngredient",
    "PurchaseUnit",
    "Unit",
    "UnitKind",
    # Catalog / index
    "AliasIndex",
    "get_catalog",
    "get_alias_index",
    "get_densities",
    # Parser
    "parse_ingredient",
    # Units
    "get_unit",
    "get_units",
    "is_known_unit",
    "convert_to_grams",
    "default_qty_grams",
    # Errors
    "KBError",
    "UnknownUnitError",
    "AmbiguousIngredientError",
]
