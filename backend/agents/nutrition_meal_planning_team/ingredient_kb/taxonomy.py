"""Closed taxonomies for ingredient annotation.

SPEC-005 §4.2. Three closed ``StrEnum``s. Downstream pattern-matches
on these values; they are load-bearing for SPEC-006 (restriction
normalization) and SPEC-007 (guardrail enforcement). Changes follow
the ``KB_VERSION`` bump policy — additions are minor, removals or
renames are major.

``gluten`` is deliberately separate from ``wheat``: spelt, barley, and
rye are gluten-bearing without being wheat, and soy sauce is wheat-
bearing (through brewing) without necessarily cross-contaminating the
way actual wheat flour does. Downstream consumers treat the two tags
independently.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class AllergenTag(str, Enum):
    """FDA Big-9 + EU-14 superset. The set SPEC-007 enforces against."""

    peanut = "peanut"
    tree_nut = "tree_nut"
    dairy = "dairy"
    egg = "egg"
    soy = "soy"
    wheat = "wheat"
    gluten = "gluten"
    fish = "fish"
    shellfish = "shellfish"
    sesame = "sesame"
    mustard = "mustard"
    celery = "celery"
    sulfites = "sulfites"
    lupin = "lupin"
    mollusc = "mollusc"


class DietaryTag(str, Enum):
    """Dietary-pattern tags used by SPEC-006 shorthand expansion.

    Vegan expands to ``forbid: {animal, dairy, egg, honey, gelatin}``.
    """

    animal = "animal"
    dairy = "dairy"
    egg = "egg"
    honey = "honey"
    gelatin = "gelatin"
    alcohol = "alcohol"
    high_fodmap = "high_fodmap"
    nightshade = "nightshade"
    gluten = "gluten"
    grain = "grain"
    legume = "legume"


class InteractionTag(str, Enum):
    """Drug-food interaction classes. Consumed by SPEC-007 guardrail.

    These are the class tags v1 interactions.yaml keys on. Adding a
    new tag requires SPEC-007 owner sign-off so the enforcement
    configuration stays reviewable.
    """

    vitamin_k_high = "vitamin_k_high"
    tyramine_high = "tyramine_high"
    potassium_high = "potassium_high"
    grapefruit = "grapefruit"
    licorice = "licorice"
    st_johns_wort = "st_johns_wort"
    very_high_fat = "very_high_fat"
    caffeine_high = "caffeine_high"
    sodium_very_high = "sodium_very_high"


# Convenience frozensets for membership tests. Source of truth is the
# enum; these are just cached projections.

FDA_BIG9: FrozenSet[AllergenTag] = frozenset(
    {
        AllergenTag.peanut,
        AllergenTag.tree_nut,
        AllergenTag.dairy,
        AllergenTag.egg,
        AllergenTag.soy,
        AllergenTag.wheat,
        AllergenTag.fish,
        AllergenTag.shellfish,
        AllergenTag.sesame,
    }
)

EU_EXTRAS: FrozenSet[AllergenTag] = frozenset(
    {
        AllergenTag.gluten,
        AllergenTag.mustard,
        AllergenTag.celery,
        AllergenTag.sulfites,
        AllergenTag.lupin,
        AllergenTag.mollusc,
    }
)
