"""SPEC-007 §4.4 step 4 — unknown medication advisory flag tests."""

from __future__ import annotations

from agents.nutrition_meal_planning_team.guardrail import (
    GuardrailResult,
    Severity,
    ViolationReason,
    check_recommendation,
)

from ._fixtures import profile_with, recipe


def test_unknown_medication_advisory_flag() -> None:
    profile = profile_with(medications=["experimental_drug_xyz"])
    result: GuardrailResult = check_recommendation(profile, recipe("chicken breast"))

    assert result.passed is True
    advisory = [f for f in result.flags if f.reason is ViolationReason.interaction_flag]
    assert len(advisory) == 1
    assert "experimental_drug_xyz" in advisory[0].detail
    assert advisory[0].severity is Severity.flag
    assert advisory[0].ingredient_raw == ""
    assert advisory[0].canonical_id is None
    assert advisory[0].tag is None


def test_multiple_unknown_medications_each_flagged() -> None:
    profile = profile_with(medications=["drug_a", "drug_b"])
    result: GuardrailResult = check_recommendation(profile, recipe("chicken breast"))

    assert result.passed is True
    advisory = [f for f in result.flags if f.reason is ViolationReason.interaction_flag]
    assert len(advisory) == 2
    flagged_meds = {f.detail for f in advisory}
    assert any("drug_a" in d for d in flagged_meds)
    assert any("drug_b" in d for d in flagged_meds)


def test_known_plus_unknown_medication() -> None:
    profile = profile_with(medications=["maoi", "unknown_med"])
    result: GuardrailResult = check_recommendation(profile, recipe("miso paste"))

    assert result.passed is False
    hard = [v for v in result.violations if v.reason is ViolationReason.interaction_hard]
    assert len(hard) >= 1
    assert any(v.tag == "tyramine_high" for v in hard)

    advisory = [f for f in result.flags if "unknown_med" in f.detail]
    assert len(advisory) == 1
    assert advisory[0].severity is Severity.flag


def test_freetext_medication_produces_advisory_flag() -> None:
    profile = profile_with(medications_freetext=["my_custom_supplement"])
    result: GuardrailResult = check_recommendation(profile, recipe("chicken breast"))

    assert result.passed is True
    advisory = [f for f in result.flags if f.reason is ViolationReason.interaction_flag]
    assert len(advisory) == 1
    assert "my_custom_supplement" in advisory[0].detail
    assert advisory[0].severity is Severity.flag


def test_freetext_plus_recognized_medication() -> None:
    profile = profile_with(medications=["maoi"], medications_freetext=["aspirin_custom"])
    result: GuardrailResult = check_recommendation(profile, recipe("miso paste"))

    assert result.passed is False
    hard = [v for v in result.violations if v.reason is ViolationReason.interaction_hard]
    assert any(v.tag == "tyramine_high" for v in hard)

    advisory = [f for f in result.flags if "aspirin_custom" in f.detail]
    assert len(advisory) == 1


def test_no_medications_no_advisory() -> None:
    result: GuardrailResult = check_recommendation(profile_with(), recipe("chicken breast"))

    advisory = [
        f
        for f in result.flags
        if f.reason in (ViolationReason.interaction_hard, ViolationReason.interaction_flag)
    ]
    assert advisory == []
