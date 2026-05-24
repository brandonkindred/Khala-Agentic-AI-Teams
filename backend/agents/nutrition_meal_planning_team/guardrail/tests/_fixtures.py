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
    ClinicalInfo,
    MealRecommendation,
    ResolvedRestriction,
    RestrictionResolution,
)
from agents.nutrition_meal_planning_team.restriction_resolver import (
    resolve_restrictions,
)


def profile_with(
    *,
    allergens: Iterable[AllergenTag] = (),
    dietary_forbid: Iterable[DietaryTag] = (),
    medications: Iterable[str] = (),
    client_id: str = "test_client",
) -> ClientProfile:
    """Build a ClientProfile with the given active tags via SPEC-006
    ``RestrictionResolution.resolved`` entries.

    Preconditions:
        ``medications`` contains recognised ``Medication`` enum values
        or free-text strings (unknown meds produce advisory flags).

    Postconditions:
        Returned profile has the specified allergens, dietary forbid tags,
        and medications set on ``clinical.medications``.
    """
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
    med_list = list(medications)
    return ClientProfile(
        client_id=client_id,
        restriction_resolution=RestrictionResolution(resolved=resolved),
        clinical=ClinicalInfo(medications=med_list) if med_list else ClinicalInfo(),
    )


def profile_from_resolver(
    *,
    allergies: Iterable[str] = (),
    dietary_needs: Iterable[str] = (),
    extra_resolved: Iterable[ResolvedRestriction] = (),
    client_id: str = "test_client",
) -> ClientProfile:
    """Build a ClientProfile via the real SPEC-006 resolver cascade.

    Use this when the test depends on resolver-attached metadata
    (e.g. ``dietary_allergen_exemptions`` from the pescatarian shorthand,
    issue #351). ``extra_resolved`` lets a test append manually-built
    rows alongside the resolver output to model "user typed pescatarian
    AND no animal" combinations.
    """
    rr = resolve_restrictions(list(allergies), list(dietary_needs))
    if extra_resolved:
        rr = rr.model_copy(update={"resolved": list(rr.resolved) + list(extra_resolved)})
    return ClientProfile(client_id=client_id, restriction_resolution=rr)


def recipe(*ingredients: str, name: str = "test recipe") -> MealRecommendation:
    return MealRecommendation(name=name, ingredients=list(ingredients))
