"""Tests for SPEC-007 §4.5/§4.6 regeneration prompt builder.

Pure-function tests — no LLM mock needed. Validates prompt content,
structure, determinism, and forbidden-list rendering.
"""

from __future__ import annotations

from agents.nutrition_meal_planning_team.agents.meal_planning_agent.regeneration_prompt import (
    REGENERATION_SYSTEM_PROMPT,
    build_regeneration_prompt,
)
from agents.nutrition_meal_planning_team.guardrail.tests._fixtures import (
    profile_from_resolver,
    profile_with,
    recipe,
)
from agents.nutrition_meal_planning_team.guardrail.violations import (
    Severity,
    Violation,
    ViolationReason,
)
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag
from agents.nutrition_meal_planning_team.models import (
    ClientProfile,
    ClinicalInfo,
    MealRecommendation,
    RestrictionResolution,
)


def _violation(
    ingredient: str = "almonds",
    tag: str | None = "tree_nut",
    reason: ViolationReason = ViolationReason.allergen,
    severity: Severity = Severity.hard_reject,
) -> Violation:
    return Violation(
        reason=reason,
        ingredient_raw=ingredient,
        canonical_id=None,
        tag=tag,
        detail=f"test: {ingredient}",
        severity=severity,
    )


# ---------------------------------------------------------------------------
# Forbidden list rendering
# ---------------------------------------------------------------------------


class TestForbiddenList:
    def test_single_violation_renders_forbidden(self):
        v = _violation("almonds", "tree_nut")
        prompt = build_regeneration_prompt(
            profile_with(allergens=[AllergenTag.tree_nut]),
            recipe("almonds", name="Almond Cake"),
            [v],
        )
        assert "FORBIDDEN INGREDIENTS" in prompt
        assert "almonds (tag: tree_nut)" in prompt

    def test_multiple_violations_all_listed(self):
        vs = [
            _violation("almonds", "tree_nut"),
            _violation("milk", "dairy"),
        ]
        prompt = build_regeneration_prompt(
            profile_with(allergens=[AllergenTag.tree_nut]),
            recipe("almonds", "milk"),
            vs,
        )
        assert "almonds (tag: tree_nut)" in prompt
        assert "milk (tag: dairy)" in prompt

    def test_none_tag_renders_ingredient_only(self):
        v = _violation(
            "mystery spice",
            None,
            reason=ViolationReason.unresolved_ingredient,
        )
        prompt = build_regeneration_prompt(
            profile_with(),
            recipe("mystery spice"),
            [v],
        )
        assert "  - mystery spice" in prompt
        assert "(tag:" not in prompt.split("mystery spice")[1].split("\n")[0]

    def test_duplicate_violations_deduplicated(self):
        v = _violation("almonds", "tree_nut")
        prompt = build_regeneration_prompt(
            profile_with(allergens=[AllergenTag.tree_nut]),
            recipe("almonds"),
            [v, v],
        )
        count = prompt.count("almonds (tag: tree_nut)")
        assert count == 1

    def test_violations_sorted_deterministically(self):
        vs_a = [
            _violation("walnuts", "tree_nut"),
            _violation("almonds", "tree_nut"),
            _violation("milk", "dairy"),
        ]
        vs_b = [
            _violation("milk", "dairy"),
            _violation("almonds", "tree_nut"),
            _violation("walnuts", "tree_nut"),
        ]
        profile = profile_with(allergens=[AllergenTag.tree_nut])
        original = recipe("walnuts", "almonds", "milk")
        prompt_a = build_regeneration_prompt(profile, original, vs_a)
        prompt_b = build_regeneration_prompt(profile, original, vs_b)
        assert prompt_a == prompt_b


# ---------------------------------------------------------------------------
# Constraints block
# ---------------------------------------------------------------------------


class TestConstraintsBlockIncluded:
    def test_vegan_profile_includes_constraints(self):
        profile = profile_from_resolver(dietary_needs=["vegan"])
        prompt = build_regeneration_prompt(
            profile,
            recipe("chicken breast"),
            [_violation("chicken breast", "animal", reason=ViolationReason.dietary_forbid)],
        )
        assert "=== DIETARY CONSTRAINTS (MUST OBEY) ===" in prompt

    def test_empty_profile_omits_constraints(self):
        profile = ClientProfile(
            client_id="test",
            restriction_resolution=RestrictionResolution(),
            clinical=ClinicalInfo(),
        )
        prompt = build_regeneration_prompt(
            profile,
            recipe("item"),
            [_violation("item", None, reason=ViolationReason.unresolved_ingredient)],
        )
        assert "=== DIETARY CONSTRAINTS" not in prompt


# ---------------------------------------------------------------------------
# Original meal context
# ---------------------------------------------------------------------------


class TestOriginalMealContext:
    def test_original_name_present(self):
        original = MealRecommendation(
            name="Almond Cake", meal_type="dessert", ingredients=["almonds", "sugar"]
        )
        prompt = build_regeneration_prompt(
            profile_with(allergens=[AllergenTag.tree_nut]),
            original,
            [_violation("almonds", "tree_nut")],
        )
        assert "Almond Cake" in prompt

    def test_original_meal_type_present(self):
        original = MealRecommendation(name="Pasta", meal_type="dinner")
        prompt = build_regeneration_prompt(
            profile_with(),
            original,
            [_violation("cheese", "dairy")],
        )
        assert "dinner" in prompt

    def test_original_ingredients_not_in_prompt(self):
        original = MealRecommendation(
            name="Nut Salad",
            meal_type="lunch",
            ingredients=["cashews", "walnuts", "lettuce"],
        )
        prompt = build_regeneration_prompt(
            profile_with(allergens=[AllergenTag.tree_nut]),
            original,
            [_violation("cashews", "tree_nut")],
        )
        assert "lettuce" not in prompt


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_100_iterations_byte_identical(self):
        profile = profile_from_resolver(
            allergies=["tree nuts"],
            dietary_needs=["vegan"],
        )
        original = MealRecommendation(
            name="Bad Meal",
            meal_type="dinner",
            ingredients=["walnuts", "cream"],
        )
        vs = [
            _violation("walnuts", "tree_nut"),
            _violation("cream", "dairy"),
        ]
        baseline = build_regeneration_prompt(profile, original, vs)
        for _ in range(100):
            assert build_regeneration_prompt(profile, original, vs) == baseline


# ---------------------------------------------------------------------------
# Prompt structure
# ---------------------------------------------------------------------------


class TestPromptStructure:
    def test_json_schema_present(self):
        prompt = build_regeneration_prompt(
            profile_with(),
            recipe("item"),
            [_violation("item", "tree_nut")],
        )
        assert '"MealRecommendation"' in prompt

    def test_json_instruction_present(self):
        prompt = build_regeneration_prompt(
            profile_with(),
            recipe("item"),
            [_violation("item", "tree_nut")],
        )
        assert "Output JSON only" in prompt

    def test_forbidden_before_instruction(self):
        prompt = build_regeneration_prompt(
            profile_with(),
            recipe("item"),
            [_violation("item", "tree_nut")],
        )
        forbidden_pos = prompt.index("FORBIDDEN INGREDIENTS")
        instruction_pos = prompt.index("Output JSON only")
        assert forbidden_pos < instruction_pos

    def test_system_prompt_is_nonempty_string(self):
        assert isinstance(REGENERATION_SYSTEM_PROMPT, str)
        assert len(REGENERATION_SYSTEM_PROMPT) > 0
