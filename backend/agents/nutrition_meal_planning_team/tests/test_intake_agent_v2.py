"""SPEC-002 W4: intake agent tests (fallback merge + new sub-objects).

Uses the pure-logic ``structural`` module so tests don't require the
``strands`` stack (kept separate per SPEC-002 W4).
"""

from __future__ import annotations

from nutrition_meal_planning_team.agents.intake_profile_agent.structural import (
    merge_profile_structural,
)
from nutrition_meal_planning_team.models import (
    ActivityLevel,
    BiometricInfo,
    ClientProfile,
    ClinicalInfo,
    HouseholdInfo,
    ProfileUpdateRequest,
    ReproductiveState,
    Sex,
)

# Alias to the historical private name the rest of the codebase uses.
_merge_profile_structural = merge_profile_structural


# --- existing behavior preserved -----------------------------------------


def test_structural_merge_preserves_existing_household():
    existing = ClientProfile(
        client_id="c1",
        household=HouseholdInfo(number_of_people=2, description="couple"),
    )
    update = ProfileUpdateRequest(dietary_needs=["vegan"])
    merged = _merge_profile_structural("c1", existing, update)
    assert merged.household.number_of_people == 2
    assert merged.household.description == "couple"
    assert merged.dietary_needs == ["vegan"]


def test_structural_merge_on_empty_profile():
    merged = _merge_profile_structural(
        "c1", None, ProfileUpdateRequest(dietary_needs=["vegetarian"])
    )
    assert merged.dietary_needs == ["vegetarian"]
    assert merged.client_id == "c1"
    assert isinstance(merged.biometrics, BiometricInfo)
    assert isinstance(merged.clinical, ClinicalInfo)


# --- SPEC-002 biometrics sub-object merge --------------------------------


def test_structural_merge_biometrics_added_to_empty_profile():
    patch = ProfileUpdateRequest(
        biometrics=BiometricInfo(sex=Sex.female, age_years=32, height_cm=168.0, weight_kg=64.5)
    )
    merged = _merge_profile_structural("c1", None, patch)
    assert merged.biometrics.sex == Sex.female
    assert merged.biometrics.age_years == 32
    assert merged.biometrics.height_cm == 168.0
    assert merged.biometrics.weight_kg == 64.5


def test_structural_merge_biometrics_overwrites_sub_object_when_provided():
    """PUT /profile with a ``biometrics`` sub-object replaces the whole block.

    Pydantic dumps a ``BiometricInfo`` with all default values populated
    (``sex=unspecified`` etc.), so the shallow merge stamps the entire
    sub-object onto the profile. Per-field biometric updates go through
    ``PATCH /profile/{id}/biometrics`` which uses the all-None
    ``BiometricPatchRequest`` shape instead.

    The only fields that survive unchanged are ``Optional`` ones the
    patch did not set (they dump as ``null`` and ``exclude_none=True``
    drops them), e.g. ``age_years`` and ``height_cm``.
    """
    existing = ClientProfile(
        client_id="c1",
        biometrics=BiometricInfo(
            sex=Sex.female,
            age_years=32,
            height_cm=168.0,
            weight_kg=64.5,
            activity_level=ActivityLevel.moderate,
        ),
    )
    patch = ProfileUpdateRequest(biometrics=BiometricInfo(weight_kg=63.0))
    merged = _merge_profile_structural("c1", existing, patch)
    # Weight arrived — survives.
    assert merged.biometrics.weight_kg == 63.0
    # Optional fields the patch left unset (None-valued) are preserved
    # because exclude_none=True drops them from the shallow-merge dict.
    assert merged.biometrics.age_years == 32
    assert merged.biometrics.height_cm == 168.0
    # Non-optional fields with defaults are dumped and stomp the existing
    # values — sub-object replace semantics, documented above.
    assert merged.biometrics.sex == Sex.unspecified
    assert merged.biometrics.activity_level == ActivityLevel.sedentary


# --- SPEC-002 clinical sub-object merge ----------------------------------


def test_structural_merge_clinical_added_to_empty_profile():
    patch = ProfileUpdateRequest(
        clinical=ClinicalInfo(
            conditions=["hypertension"],
            medications=["warfarin"],
            reproductive_state=ReproductiveState.none,
            ed_history_flag=False,
        )
    )
    merged = _merge_profile_structural("c1", None, patch)
    assert merged.clinical.conditions == ["hypertension"]
    assert merged.clinical.medications == ["warfarin"]
    assert merged.clinical.ed_history_flag is False


def test_structural_merge_ed_flag_survives_no_update():
    existing = ClientProfile(
        client_id="c1",
        clinical=ClinicalInfo(ed_history_flag=True),
    )
    # Patch touches a completely different area.
    patch = ProfileUpdateRequest(dietary_needs=["vegan"])
    merged = _merge_profile_structural("c1", existing, patch)
    assert merged.clinical.ed_history_flag is True
    assert merged.dietary_needs == ["vegan"]


# --- SPEC-006: resolver hook ---------------------------------------------


def test_apply_resolver_flag_off_is_noop(monkeypatch):
    """When the flag is unset, ``apply_resolver`` returns the profile
    untouched."""
    from nutrition_meal_planning_team.agents.intake_profile_agent.restriction_hook import (
        apply_resolver,
    )

    monkeypatch.delenv("NUTRITION_RESTRICTION_RESOLVER", raising=False)
    p = ClientProfile(
        client_id="c1",
        allergies_and_intolerances=["cashew"],
        dietary_needs=["vegan"],
    )
    out = apply_resolver(p)
    assert out.restriction_resolution.resolved == []
    assert out.restriction_resolution.kb_version == ""


def test_apply_resolver_flag_on_populates_resolution(monkeypatch):
    """When the flag is on, ``apply_resolver`` runs the cascade and
    populates ``restriction_resolution`` from the raw lists.
    """
    from nutrition_meal_planning_team.agents.intake_profile_agent.restriction_hook import (
        apply_resolver,
    )

    monkeypatch.setenv("NUTRITION_RESTRICTION_RESOLVER", "1")
    p = ClientProfile(
        client_id="c1",
        allergies_and_intolerances=["cashew"],
        dietary_needs=["vegan"],
    )
    out = apply_resolver(p)
    raws = {r.raw for r in out.restriction_resolution.resolved}
    assert raws == {"cashew", "vegan"}
    assert out.restriction_resolution.kb_version  # non-empty
