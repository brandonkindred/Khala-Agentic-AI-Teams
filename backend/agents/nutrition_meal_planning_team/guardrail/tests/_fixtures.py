"""Lightweight helpers for building ClientProfile + MealRecommendation
fixtures in guardrail unit tests.
"""

from __future__ import annotations

from typing import Iterable

from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import (
    AllergenTag,
    DietaryTag,
)
from agents.nutrition_meal_planning_team.models import (
    ClientProfile,
    MealRecommendation,
    ResolvedRestriction,
    RestrictionResolution,
)


def profile_with(
    *,
    allergens: Iterable[AllergenTag] = (),
    dietary_forbid: Iterable[DietaryTag] = (),
    client_id: str = "test_client",
) -> ClientProfile:
    """Build a ClientProfile with the given active tags via SPEC-006
    ``RestrictionResolution.resolved`` entries."""
    resolved: list[ResolvedRestriction] = []
    for tag in allergens:
        resolved.append(
            ResolvedRestriction(
                raw=tag.value,
                allergen_tags=[tag],
            )
        )
    for tag in dietary_forbid:
        resolved.append(
            ResolvedRestriction(
                raw=tag.value,
                dietary_tags_forbid=[tag],
            )
        )
    return ClientProfile(
        client_id=client_id,
        restriction_resolution=RestrictionResolution(resolved=resolved),
    )


def recipe(*ingredients: str, name: str = "test recipe") -> MealRecommendation:
    return MealRecommendation(name=name, ingredients=list(ingredients))
