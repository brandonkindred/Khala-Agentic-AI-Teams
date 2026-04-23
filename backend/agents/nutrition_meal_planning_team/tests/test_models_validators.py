"""SPEC-002 W1: validator tests for ClientProfile additions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nutrition_meal_planning_team.models import (
    PROFILE_SCHEMA_VERSION,
    ActivityLevel,
    BiometricInfo,
    BiometricPatchRequest,
    ClientProfile,
    ClinicalInfo,
    ClinicalPatchRequest,
    ClinicianOverrideRequest,
    GoalsInfo,
    ReproductiveState,
    Sex,
)

# --- BiometricInfo range bounds ------------------------------------------


def test_defaults_are_sane():
    b = BiometricInfo()
    assert b.sex == Sex.unspecified
    assert b.age_years is None
    assert b.height_cm is None
    assert b.weight_kg is None
    assert b.activity_level == ActivityLevel.sedentary
    assert b.timezone == "UTC"
    assert b.preferred_units == "metric"


@pytest.mark.parametrize("age", [1, 0, 121, 200])
def test_age_rejected_outside_range(age):
    with pytest.raises(ValidationError):
        BiometricInfo(age_years=age)


@pytest.mark.parametrize("age", [2, 18, 60, 120])
def test_age_accepted_within_range(age):
    assert BiometricInfo(age_years=age).age_years == age


@pytest.mark.parametrize("h", [49, 261, 0, -5])
def test_height_rejected_outside_range(h):
    with pytest.raises(ValidationError):
        BiometricInfo(height_cm=h)


@pytest.mark.parametrize("h", [50, 150, 180, 260])
def test_height_accepted_within_range(h):
    assert BiometricInfo(height_cm=h).height_cm == h


@pytest.mark.parametrize("w", [19, 401, 0, -10])
def test_weight_rejected_outside_range(w):
    with pytest.raises(ValidationError):
        BiometricInfo(weight_kg=w)


@pytest.mark.parametrize("w", [20, 70, 150, 400])
def test_weight_accepted_within_range(w):
    assert BiometricInfo(weight_kg=w).weight_kg == w


@pytest.mark.parametrize("bf", [2, 76, 100])
def test_body_fat_rejected_outside_range(bf):
    with pytest.raises(ValidationError):
        BiometricInfo(body_fat_pct=bf)


@pytest.mark.parametrize("bf", [3, 20, 50, 75])
def test_body_fat_accepted_within_range(bf):
    assert BiometricInfo(body_fat_pct=bf).body_fat_pct == bf


def test_timezone_invalid_rejected():
    with pytest.raises(ValidationError):
        BiometricInfo(timezone="Not/A/Zone")


def test_timezone_utc_accepted():
    assert BiometricInfo(timezone="UTC").timezone == "UTC"


def test_timezone_america_new_york_accepted():
    assert BiometricInfo(timezone="America/New_York").timezone == "America/New_York"


def test_preferred_units_validation():
    assert BiometricInfo(preferred_units="imperial").preferred_units == "imperial"
    with pytest.raises(ValidationError):
        BiometricInfo(preferred_units="smoots")


# --- GoalsInfo -----------------------------------------------------------


def test_goals_rate_rejected_above_one():
    with pytest.raises(ValidationError):
        GoalsInfo(rate_kg_per_week=1.5)


def test_goals_rate_accepted_within_range():
    g = GoalsInfo(rate_kg_per_week=0.45)
    assert g.rate_kg_per_week == 0.45


def test_goals_target_weight_rejected_outside_range():
    with pytest.raises(ValidationError):
        GoalsInfo(target_weight_kg=10)
    with pytest.raises(ValidationError):
        GoalsInfo(target_weight_kg=500)


def test_goals_target_weight_accepted():
    g = GoalsInfo(target_weight_kg=75.0)
    assert g.target_weight_kg == 75.0


# --- ClientProfile -------------------------------------------------------


def test_client_profile_has_new_fields_and_defaults():
    p = ClientProfile(client_id="c1")
    assert isinstance(p.biometrics, BiometricInfo)
    assert isinstance(p.clinical, ClinicalInfo)
    assert p.schema_version == PROFILE_SCHEMA_VERSION
    assert p.profile_version == 1


def test_client_profile_roundtrips_json():
    p = ClientProfile(
        client_id="c2",
        biometrics=BiometricInfo(
            sex=Sex.female,
            age_years=32,
            height_cm=168.0,
            weight_kg=64.5,
            activity_level=ActivityLevel.moderate,
        ),
        clinical=ClinicalInfo(
            conditions=["hypertension"],
            medications=["acei_arb"],
            reproductive_state=ReproductiveState.none,
            ed_history_flag=False,
        ),
    )
    payload = p.model_dump()
    restored = ClientProfile.model_validate(payload)
    assert restored.biometrics.sex == Sex.female
    assert restored.clinical.conditions == ["hypertension"]
    assert restored.clinical.medications == ["acei_arb"]


# --- BiometricPatchRequest -----------------------------------------------


def test_biometric_patch_all_none_is_valid():
    p = BiometricPatchRequest()
    assert p.height_cm is None and p.weight_kg is None


def test_biometric_patch_rejects_implausible():
    with pytest.raises(ValidationError):
        BiometricPatchRequest(weight_kg=500)
    with pytest.raises(ValidationError):
        BiometricPatchRequest(height_cm=20)
    with pytest.raises(ValidationError):
        BiometricPatchRequest(weight_lb=1000)


def test_biometric_patch_accepts_imperial_inputs():
    p = BiometricPatchRequest(height_ft=5, height_in=10, weight_lb=165)
    assert p.height_ft == 5 and p.height_in == 10
    assert p.weight_lb == 165


# --- ClinicalPatchRequest ------------------------------------------------


def test_clinical_patch_nullable_fields():
    p = ClinicalPatchRequest()
    assert p.conditions is None
    assert p.medications is None
    assert p.ed_history_flag is None


def test_clinical_patch_accepts_lists():
    p = ClinicalPatchRequest(
        conditions=["hypertension", "weird-unknown-thing"],
        medications=["warfarin"],
    )
    assert "hypertension" in p.conditions
    assert p.medications == ["warfarin"]


# --- ClinicianOverrideRequest --------------------------------------------


def test_clinician_override_defaults():
    r = ClinicianOverrideRequest()
    assert r.overrides == {}
    assert r.author == "admin"


def test_clinician_override_roundtrip():
    r = ClinicianOverrideRequest(
        overrides={"bmi_floor": 19.5, "sodium_cap_mg": 1500.0},
        reason="dietitian guidance",
        author="dietitian-1234",
    )
    assert r.overrides["bmi_floor"] == 19.5
    assert r.author == "dietitian-1234"


# --- SPEC-006: restriction resolution model ------------------------------


def test_client_profile_default_has_empty_restriction_resolution():
    from nutrition_meal_planning_team.models import RestrictionResolution

    p = ClientProfile(client_id="c")
    assert isinstance(p.restriction_resolution, RestrictionResolution)
    assert p.restriction_resolution.resolved == []
    assert p.restriction_resolution.ambiguous == []
    assert p.restriction_resolution.unresolved == []


def test_restriction_resolution_active_allergen_union():
    from nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag
    from nutrition_meal_planning_team.models import (
        AmbiguousRestriction,
        ResolvedRestriction,
        RestrictionResolution,
    )

    rr = RestrictionResolution(
        resolved=[
            ResolvedRestriction(
                raw="cashew",
                allergen_tags=[AllergenTag.tree_nut],
                confidence=1.0,
                rule="exact_alias",
            )
        ],
        ambiguous=[
            AmbiguousRestriction(
                raw="nuts",
                candidates=[
                    ResolvedRestriction(raw="nuts", allergen_tags=[AllergenTag.peanut]),
                    ResolvedRestriction(raw="nuts", allergen_tags=[AllergenTag.tree_nut]),
                ],
                question="?",
            )
        ],
    )
    active = rr.active_allergen_tags()
    # Default-strict: union of resolved + strictest candidate tags.
    assert AllergenTag.peanut in active
    assert AllergenTag.tree_nut in active


def test_client_profile_roundtrips_restriction_resolution_through_json():
    from nutrition_meal_planning_team.ingredient_kb.taxonomy import DietaryTag
    from nutrition_meal_planning_team.models import (
        ResolvedRestriction,
        RestrictionResolution,
    )

    rr = RestrictionResolution(
        resolved=[
            ResolvedRestriction(
                raw="vegan",
                dietary_tags_forbid=[DietaryTag.animal, DietaryTag.dairy],
                source="shorthand",
                rule="shorthand",
                confidence=1.0,
            )
        ],
        kb_version="1.0.0",
        resolved_at="2026-04-23T00:00:00+00:00",
    )
    p = ClientProfile(client_id="c", restriction_resolution=rr)
    p2 = ClientProfile.model_validate(p.model_dump())
    assert p2.restriction_resolution.kb_version == "1.0.0"
    assert p2.restriction_resolution.resolved[0].raw == "vegan"
    assert DietaryTag.dairy in p2.restriction_resolution.resolved[0].dietary_tags_forbid
