"""Tests for SPEC-007 §4.6 prompt constraints block.

Snapshot-style assertions: verify that known profiles produce
constraint blocks containing expected sections and tags. Uses
substring checks for maintainability as prompt wording evolves.
"""

from __future__ import annotations

import pytest
from agents.nutrition_meal_planning_team.agents.meal_planning_agent.prompt_constraints import (
    render_constraints_block,
)
from agents.nutrition_meal_planning_team.guardrail.tests._fixtures import (
    profile_from_resolver,
    profile_with,
)
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag
from agents.nutrition_meal_planning_team.models import (
    AmbiguousRestriction,
    ClinicalInfo,
    ResolvedRestriction,
    RestrictionResolution,
)


class TestEmptyResolution:
    def test_empty_resolution_returns_empty_string(self):
        result = render_constraints_block(RestrictionResolution(), ClinicalInfo())
        assert result == ""

    def test_empty_lists_returns_empty_string(self):
        rr = RestrictionResolution(resolved=[], ambiguous=[], unresolved=[])
        result = render_constraints_block(rr, ClinicalInfo(medications=[]))
        assert result == ""


class TestVegan:
    @pytest.fixture()
    def block(self):
        p = profile_from_resolver(dietary_needs=["vegan"])
        return render_constraints_block(p.restriction_resolution, p.clinical)

    def test_contains_header(self, block):
        assert "=== DIETARY CONSTRAINTS (MUST OBEY) ===" in block

    def test_contains_footer(self, block):
        assert "=== END DIETARY CONSTRAINTS ===" in block

    def test_forbidden_dietary_categories(self, block):
        assert "FORBIDDEN dietary categories" in block
        for tag in ["animal", "dairy", "egg", "gelatin", "honey"]:
            assert f"  - {tag}" in block

    def test_no_forbidden_allergens_section(self, block):
        assert "FORBIDDEN allergens" not in block

    def test_shorthand_listed(self, block):
        assert "DIETARY SHORTHANDS" in block
        assert "vegan" in block

    def test_unsure_warning(self, block):
        assert "If you are unsure" in block


class TestTreeNutAllergy:
    @pytest.fixture()
    def block(self):
        p = profile_with(allergens=[AllergenTag.tree_nut])
        return render_constraints_block(p.restriction_resolution, p.clinical)

    def test_forbidden_allergens(self, block):
        assert "FORBIDDEN allergens" in block
        assert "  - tree_nut" in block

    def test_no_dietary_section(self, block):
        assert "FORBIDDEN dietary categories" not in block


class TestWarfarinPatient:
    @pytest.fixture()
    def block(self):
        rr = RestrictionResolution()
        clinical = ClinicalInfo(medications=["warfarin"])
        return render_constraints_block(rr, clinical)

    def test_flag_only_section(self, block):
        assert "FLAG-ONLY" in block
        assert "vitamin_k_high" in block
        assert "warfarin" in block

    def test_no_forbidden_section(self, block):
        assert "FORBIDDEN allergens" not in block
        assert "FORBIDDEN dietary" not in block


class TestMaoiPatient:
    @pytest.fixture()
    def block(self):
        rr = RestrictionResolution()
        clinical = ClinicalInfo(medications=["maoi"])
        return render_constraints_block(rr, clinical)

    def test_tyramine_in_forbidden(self, block):
        assert "FORBIDDEN allergens" in block
        assert "tyramine_high" in block

    def test_tyramine_not_in_flag_only(self, block):
        assert "FLAG-ONLY" not in block


class TestPescatarian:
    @pytest.fixture()
    def block(self):
        p = profile_from_resolver(dietary_needs=["pescatarian"])
        return render_constraints_block(p.restriction_resolution, p.clinical)

    def test_forbidden_animal(self, block):
        assert "FORBIDDEN dietary categories" in block
        assert "  - animal" in block

    def test_exemptions_section(self, block):
        assert "EXEMPTIONS" in block
        assert "fish" in block
        assert "shellfish" in block
        assert "ARE allowed" in block

    def test_shorthand_listed(self, block):
        assert "DIETARY SHORTHANDS" in block
        assert "pescatarian" in block


class TestPescatarianWithFishAllergy:
    """Paradox case: pescatarian who is also allergic to fish."""

    @pytest.fixture()
    def block(self):
        p = profile_from_resolver(dietary_needs=["pescatarian"], allergies=["fish"])
        return render_constraints_block(p.restriction_resolution, p.clinical)

    def test_fish_in_forbidden_allergens(self, block):
        assert "FORBIDDEN allergens" in block
        assert "  - fish" in block

    def test_animal_in_forbidden_dietary(self, block):
        assert "FORBIDDEN dietary categories" in block
        assert "  - animal" in block


class TestCombinedProfile:
    """Vegan + tree-nut allergy + warfarin: all section types populated."""

    @pytest.fixture()
    def block(self):
        p = profile_from_resolver(
            dietary_needs=["vegan"],
            allergies=["tree nut"],
        )
        clinical = ClinicalInfo(medications=["warfarin"])
        return render_constraints_block(p.restriction_resolution, clinical)

    def test_allergens_present(self, block):
        assert "FORBIDDEN allergens" in block
        assert "  - tree_nut" in block

    def test_dietary_present(self, block):
        assert "FORBIDDEN dietary categories" in block

    def test_flag_present(self, block):
        assert "FLAG-ONLY" in block
        assert "vitamin_k_high" in block

    def test_shorthands_present(self, block):
        assert "DIETARY SHORTHANDS" in block
        assert "vegan" in block

    def test_sections_in_order(self, block):
        allergen_pos = block.index("FORBIDDEN allergens")
        dietary_pos = block.index("FORBIDDEN dietary")
        flag_pos = block.index("FLAG-ONLY")
        assert allergen_pos < dietary_pos < flag_pos


class TestSoftConstraint:
    def test_keto_soft_constraint(self):
        rr = RestrictionResolution(
            resolved=[
                ResolvedRestriction(
                    raw="keto",
                    source="shorthand",
                    soft_constraint="low_carb",
                ),
            ]
        )
        block = render_constraints_block(rr, ClinicalInfo())
        assert "FLAG-ONLY" in block
        assert "low_carb" in block
        assert "soft preference" in block


class TestAmbiguousRestriction:
    def test_ambiguous_shows_warning(self):
        rr = RestrictionResolution(
            ambiguous=[
                AmbiguousRestriction(
                    raw="nuts",
                    candidates=[
                        ResolvedRestriction(raw="peanut", allergen_tags=[AllergenTag.peanut]),
                        ResolvedRestriction(raw="tree_nut", allergen_tags=[AllergenTag.tree_nut]),
                    ],
                    question="Do you mean peanuts, tree nuts, or both?",
                ),
            ]
        )
        block = render_constraints_block(rr, ClinicalInfo())
        assert "WARNINGS" in block
        assert '"nuts" is ambiguous' in block

    def test_ambiguous_applies_strict_allergens(self):
        rr = RestrictionResolution(
            ambiguous=[
                AmbiguousRestriction(
                    raw="nuts",
                    candidates=[
                        ResolvedRestriction(raw="peanut", allergen_tags=[AllergenTag.peanut]),
                        ResolvedRestriction(raw="tree_nut", allergen_tags=[AllergenTag.tree_nut]),
                    ],
                    question="Do you mean peanuts, tree nuts, or both?",
                ),
            ]
        )
        block = render_constraints_block(rr, ClinicalInfo())
        assert "FORBIDDEN allergens" in block
        assert "peanut" in block
        assert "tree_nut" in block


class TestUnresolvedRestriction:
    def test_unresolved_shows_warning(self):
        rr = RestrictionResolution(unresolved=["acai berry extract"])
        block = render_constraints_block(rr, ClinicalInfo())
        assert "WARNINGS" in block
        assert '"acai berry extract" could not be resolved' in block


class TestFreetextMedications:
    def test_unrecognized_medications_warning(self):
        clinical = ClinicalInfo(medications_freetext=["herbal supplement X"])
        rr = RestrictionResolution()
        block = render_constraints_block(rr, clinical)
        assert "WARNINGS" in block
        assert "Unrecognized medications" in block
        assert "herbal supplement X" in block


class TestDeterminism:
    def test_100_iterations_byte_identical(self):
        p = profile_from_resolver(
            dietary_needs=["vegan"],
            allergies=["tree nut", "peanut"],
        )
        clinical = ClinicalInfo(medications=["warfarin", "maoi"])

        first = render_constraints_block(p.restriction_resolution, clinical)
        assert first != ""

        for _ in range(99):
            again = render_constraints_block(p.restriction_resolution, clinical)
            assert again == first


class TestMultipleMedicationsSameTag:
    def test_overlapping_flag_tags_deduplicated(self):
        clinical = ClinicalInfo(medications=["acei_arb", "k_sparing_diuretic"])
        rr = RestrictionResolution()
        block = render_constraints_block(rr, clinical)
        assert "FLAG-ONLY" in block
        assert "potassium_high" in block
        assert "acei_arb" in block
        assert "k_sparing_diuretic" in block
        lines = [line for line in block.split("\n") if "potassium_high" in line]
        assert len(lines) == 1
