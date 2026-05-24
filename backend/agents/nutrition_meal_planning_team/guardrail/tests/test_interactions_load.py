"""SPEC-007 W3 — interactions.yaml loader behavior tests.

Validates that ``load_interactions`` returns well-shaped, frozen
``InteractionPolicy`` entries for every medication class, and that
``get_interaction_policies`` handles known/unknown medication strings
correctly.
"""

from __future__ import annotations

import dataclasses

import pytest
from agents.nutrition_meal_planning_team.clinical_taxonomy import Medication
from agents.nutrition_meal_planning_team.guardrail.interactions import (
    EMPTY_POLICY,
    InteractionPolicy,
    get_interaction_policies,
    load_interactions,
)
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import InteractionTag


class TestLoadInteractions:
    def test_returns_dict(self) -> None:
        result = load_interactions()
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_every_medication_has_policy(self) -> None:
        result = load_interactions()
        for med in Medication:
            assert med.value in result, f"missing policy for {med.value}"

    def test_no_extra_keys(self) -> None:
        result = load_interactions()
        med_values = {m.value for m in Medication}
        assert set(result.keys()) == med_values

    def test_policy_is_frozen_dataclass(self) -> None:
        result = load_interactions()
        policy = result["warfarin"]
        assert isinstance(policy, InteractionPolicy)
        assert dataclasses.is_dataclass(policy)
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.note = "mutated"  # type: ignore[misc]

    def test_hard_and_flag_are_frozensets(self) -> None:
        result = load_interactions()
        for med, policy in result.items():
            assert isinstance(policy.hard, frozenset), f"{med}.hard is not frozenset"
            assert isinstance(policy.flag, frozenset), f"{med}.flag is not frozenset"

    def test_hard_and_flag_disjoint(self) -> None:
        result = load_interactions()
        for med, policy in result.items():
            overlap = policy.hard & policy.flag
            assert not overlap, f"{med} has tags in both hard and flag: {overlap}"

    def test_medication_field_matches_key(self) -> None:
        result = load_interactions()
        for med, policy in result.items():
            assert policy.medication == med


class TestPinnedPolicies:
    def test_warfarin_policy(self) -> None:
        policy = load_interactions()["warfarin"]
        assert policy.hard == frozenset()
        assert policy.flag == frozenset({InteractionTag.vitamin_k_high})
        assert "vitamin K" in policy.note

    def test_maoi_hard_rejects_tyramine(self) -> None:
        policy = load_interactions()["maoi"]
        assert InteractionTag.tyramine_high in policy.hard
        assert policy.flag == frozenset()

    def test_maoi_is_the_only_hard_rejection(self) -> None:
        result = load_interactions()
        hard_meds = [med for med, p in result.items() if p.hard]
        assert hard_meds == ["maoi"]

    def test_statin_flags_grapefruit(self) -> None:
        policy = load_interactions()["statin"]
        assert InteractionTag.grapefruit in policy.flag

    def test_ssri_flags_st_johns_wort(self) -> None:
        policy = load_interactions()["ssri"]
        assert InteractionTag.st_johns_wort in policy.flag

    def test_acei_arb_flags_potassium(self) -> None:
        policy = load_interactions()["acei_arb"]
        assert InteractionTag.potassium_high in policy.flag

    def test_glp1_flags_very_high_fat(self) -> None:
        policy = load_interactions()["glp1"]
        assert InteractionTag.very_high_fat in policy.flag

    def test_metformin_has_no_tags(self) -> None:
        policy = load_interactions()["metformin"]
        assert policy.hard == frozenset()
        assert policy.flag == frozenset()


class TestGetInteractionPolicies:
    def test_known_medications(self) -> None:
        known, unknown = get_interaction_policies(["warfarin", "maoi"])
        assert "warfarin" in known
        assert "maoi" in known
        assert unknown == []

    def test_unknown_medication(self) -> None:
        known, unknown = get_interaction_policies(["warfarin", "aspirin_unknown"])
        assert "warfarin" in known
        assert "aspirin_unknown" not in known
        assert unknown == ["aspirin_unknown"]

    def test_all_unknown(self) -> None:
        known, unknown = get_interaction_policies(["aspirin_unknown"])
        assert known == {}
        assert unknown == ["aspirin_unknown"]

    def test_empty_input(self) -> None:
        known, unknown = get_interaction_policies([])
        assert known == {}
        assert unknown == []

    def test_duplicate_medication(self) -> None:
        known, unknown = get_interaction_policies(["warfarin", "warfarin"])
        assert len(known) == 1
        assert unknown == []


class TestEmptyPolicy:
    def test_sentinel_shape(self) -> None:
        assert EMPTY_POLICY.medication == ""
        assert EMPTY_POLICY.hard == frozenset()
        assert EMPTY_POLICY.flag == frozenset()
        assert EMPTY_POLICY.note == ""
