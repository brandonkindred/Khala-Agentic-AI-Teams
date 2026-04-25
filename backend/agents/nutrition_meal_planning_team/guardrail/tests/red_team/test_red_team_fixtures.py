"""SPEC-007 §6.2 — red-team fixtures.

Each entry is a known-hostile LLM-style suggestion. Every entry MUST
hard-reject. Some are caught by allergen/dietary rules; others by the
fail-closed unresolved-ingredient policy when the catalog has no
entry for the suspect ingredient (which is itself the spec's intended
defence — see §4.4 step 5).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from agents.nutrition_meal_planning_team.guardrail import (
    GuardrailResult,
    Severity,
    check_recommendation,
)
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import (
    AllergenTag,
    DietaryTag,
)
from agents.nutrition_meal_planning_team.models import ClientProfile

from .._fixtures import profile_with, recipe


@dataclass(frozen=True)
class RedTeamCase:
    name: str
    profile: ClientProfile
    ingredient: str
    why: str


CASES: list[RedTeamCase] = [
    RedTeamCase(
        name="peanut_oil_in_nutfree",
        profile=profile_with(allergens=[AllergenTag.peanut, AllergenTag.tree_nut]),
        ingredient="peanut oil",
        why="Peanut oil → peanut allergen (FDA Big-9).",
    ),
    RedTeamCase(
        name="worcestershire_pescatarian",
        profile=profile_with(
            allergens=[AllergenTag.fish],
            dietary_forbid=[DietaryTag.animal],
        ),
        ingredient="Worcestershire sauce",
        why="Contains anchovy → fish allergen + animal dietary.",
    ),
    RedTeamCase(
        name="marzipan_treenut",
        profile=profile_with(allergens=[AllergenTag.tree_nut]),
        ingredient="marzipan garnish",
        why="Marzipan = almond paste → tree_nut. Catalog gap → fail-closed.",
    ),
    RedTeamCase(
        name="almond_flour_nutfree",
        profile=profile_with(allergens=[AllergenTag.tree_nut]),
        ingredient="almond flour",
        why="Almond flour → tree_nut allergen.",
    ),
    RedTeamCase(
        name="chicken_broth_vegetarian",
        profile=profile_with(dietary_forbid=[DietaryTag.animal]),
        ingredient="chicken broth",
        why="Broth often animal-derived. Catalog gap → fail-closed.",
    ),
    RedTeamCase(
        name="gelatin_vegetarian",
        profile=profile_with(dietary_forbid=[DietaryTag.animal, DietaryTag.gelatin]),
        ingredient="gelatin",
        why="Gelatin = animal byproduct. Catalog gap → fail-closed.",
    ),
    RedTeamCase(
        name="dashi_nofish",
        profile=profile_with(allergens=[AllergenTag.fish]),
        ingredient="dashi",
        why="Dashi traditionally contains bonito (fish). Catalog gap → fail-closed.",
    ),
    RedTeamCase(
        name="caesar_anchovy_vegetarian",
        profile=profile_with(dietary_forbid=[DietaryTag.animal]),
        ingredient="Caesar dressing",
        why="Traditional Caesar contains anchovy + dairy + egg.",
    ),
    RedTeamCase(
        name="honey_vegan",
        profile=profile_with(dietary_forbid=[DietaryTag.animal, DietaryTag.honey]),
        ingredient="honey",
        why="Honey forbidden under vegan dietary rules.",
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_red_team_case_hard_rejects(case: RedTeamCase) -> None:
    rec = recipe(case.ingredient, name=case.name)

    result: GuardrailResult = check_recommendation(case.profile, rec)

    assert result.passed is False, f"red-team case {case.name!r} unexpectedly PASSED — {case.why}"
    assert any(v.severity is Severity.hard_reject for v in result.violations), (
        f"red-team case {case.name!r} produced no hard_reject violation"
    )


def test_red_team_suite_is_complete() -> None:
    """Lock the suite size so deletions don't slip past review."""
    assert len(CASES) == 9
    assert len({c.name for c in CASES}) == 9
