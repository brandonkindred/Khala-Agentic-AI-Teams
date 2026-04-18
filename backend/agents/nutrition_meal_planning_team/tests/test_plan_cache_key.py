"""SPEC-004 §4.5 / §6.1 — profile_cache_vector stability tests.

Unit-level: runs the helper directly without Postgres so we can
exercise the "which edits invalidate" matrix without DB round-trips.
"""

from __future__ import annotations

from nutrition_meal_planning_team.models import (
    ActivityLevel,
    BiometricInfo,
    ClientProfile,
    ClinicalInfo,
    GoalsInfo,
    HouseholdInfo,
    LifestyleInfo,
    PreferencesInfo,
    Sex,
)
from nutrition_meal_planning_team.shared.nutrition_plan_store import (
    profile_cache_vector,
)


def _complete_profile(**bio_overrides) -> ClientProfile:
    base = dict(
        sex=Sex.female,
        age_years=32,
        height_cm=168.0,
        weight_kg=64.5,
        activity_level=ActivityLevel.moderate,
    )
    base.update(bio_overrides)
    return ClientProfile(
        client_id="c",
        biometrics=BiometricInfo(**base),
        goals=GoalsInfo(goal_type="maintain"),
    )


# --- Determinism ---------------------------------------------------------


def test_vector_is_deterministic():
    p1 = _complete_profile()
    p2 = _complete_profile()
    assert profile_cache_vector(p1) == profile_cache_vector(p2)


def test_vector_stable_across_irrelevant_fields():
    """Editing preferences / household / lifestyle must NOT change the vector."""
    base = _complete_profile()
    v0 = profile_cache_vector(base)

    # Edit household description: not a calculator input.
    base.household = HouseholdInfo(number_of_people=4, description="family of 4")
    assert profile_cache_vector(base) == v0

    # Edit cuisine preferences: not a calculator input.
    base.preferences = PreferencesInfo(cuisines_liked=["italian", "japanese"])
    assert profile_cache_vector(base) == v0

    # Edit lunch context: not a calculator input (feeds SPEC-010 meal planner).
    base.lifestyle = LifestyleInfo(lunch_context="office")
    assert profile_cache_vector(base) == v0


def test_vector_stable_across_bookkeeping_fields():
    """client_id, updated_at, schema_version should not flip the vector."""
    p1 = _complete_profile()
    v = profile_cache_vector(p1)
    p1.client_id = "different-client"
    p1.updated_at = "2026-04-18T00:00:00Z"
    p1.schema_version = "99.99"
    assert profile_cache_vector(p1) == v


def test_vector_excludes_preferred_units_and_measured_at():
    """Cosmetic bio fields must not invalidate the cache."""
    base = _complete_profile()
    v0 = profile_cache_vector(base)
    base.biometrics.preferred_units = "imperial"
    base.biometrics.measured_at = "2026-04-17T12:00:00Z"
    assert profile_cache_vector(base) == v0


# --- Invalidation --------------------------------------------------------


def test_weight_change_invalidates():
    v0 = profile_cache_vector(_complete_profile(weight_kg=64.5))
    v1 = profile_cache_vector(_complete_profile(weight_kg=63.5))
    assert v0 != v1


def test_height_change_invalidates():
    v0 = profile_cache_vector(_complete_profile(height_cm=168.0))
    v1 = profile_cache_vector(_complete_profile(height_cm=170.0))
    assert v0 != v1


def test_activity_level_change_invalidates():
    v0 = profile_cache_vector(_complete_profile(activity_level=ActivityLevel.moderate))
    v1 = profile_cache_vector(_complete_profile(activity_level=ActivityLevel.active))
    assert v0 != v1


def test_goal_type_change_invalidates():
    p = _complete_profile()
    v0 = profile_cache_vector(p)
    p.goals = GoalsInfo(goal_type="lose_weight", rate_kg_per_week=0.5)
    assert profile_cache_vector(p) != v0


def test_clinical_condition_add_invalidates():
    p = _complete_profile()
    v0 = profile_cache_vector(p)
    p.clinical = ClinicalInfo(conditions=["hypertension"])
    assert profile_cache_vector(p) != v0


def test_ed_flag_change_invalidates():
    p = _complete_profile()
    v0 = profile_cache_vector(p)
    p.clinical = ClinicalInfo(ed_history_flag=True)
    assert profile_cache_vector(p) != v0


def test_dietary_needs_change_invalidates():
    """Dietary needs affect macro allocation (keto fat share, etc.)."""
    p = _complete_profile()
    v0 = profile_cache_vector(p)
    p.dietary_needs = ["keto"]
    assert profile_cache_vector(p) != v0


def test_dietary_needs_order_stable():
    """Sorted-set semantics: order of dietary_needs doesn't flip the vector."""
    p_ab = _complete_profile()
    p_ab.dietary_needs = ["vegan", "low_sodium"]
    p_ba = _complete_profile()
    p_ba.dietary_needs = ["low_sodium", "vegan"]
    assert profile_cache_vector(p_ab) == profile_cache_vector(p_ba)


def test_goals_notes_does_not_invalidate():
    """Narrative goal notes are not calculator input."""
    p = _complete_profile()
    v0 = profile_cache_vector(p)
    p.goals = GoalsInfo(goal_type="maintain", notes="trying to be more consistent")
    assert profile_cache_vector(p) == v0
