"""SPEC-004 §6.2 — orchestrator _build_nutrition_plan + safety invariants.

These tests exercise the plan-building paths without talking to
Postgres or an LLM. We stub the NutritionistAgent with a fake that
returns canned narrative / guidance payloads, pointing to the
orchestrator's real calculator and safety-invariant code paths.

The orchestrator module imports ``llm_service.get_strands_model`` at
top-level, which transitively requires ``strands-agents`` — not
installed in every dev environment. Skip the whole module when it's
missing so pure-unit runs pass even without the full LLM stack.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "strands",
    reason="orchestrator requires strands-agents (install via make install-dev)",
)

from nutrition_meal_planning_team.agents.nutritionist_agent import (  # noqa: E402
    GuidanceOnlyPayload,
    NarrativePayload,
)
from nutrition_meal_planning_team.models import (
    ActivityLevel,
    BiometricInfo,
    ClientProfile,
    ClinicalInfo,
    GoalsInfo,
    PlanCohort,
    ReproductiveState,
    Sex,
)
from nutrition_meal_planning_team.nutrition_calc import CALCULATOR_VERSION

# --- Fakes ---------------------------------------------------------------


class _FakeNutritionist:
    """Canned-response narrator that never calls an LLM."""

    def __init__(
        self,
        narrative: NarrativePayload | None = None,
        guidance: GuidanceOnlyPayload | None = None,
    ) -> None:
        self._narrative = narrative or NarrativePayload(
            balance_guidelines=["eat more veg"],
            foods_to_emphasize=["leafy greens"],
            foods_to_avoid=["deep-fried"],
            notes="distribute protein across meals",
            summary="Balanced maintenance plan.",
        )
        self._guidance = guidance

    def narrate_plan(self, profile, targets, rationale_dict):
        return self._narrative

    def narrate_general_guidance(self, profile, guidance_key, default_clinician_note):
        return self._guidance or GuidanceOnlyPayload(
            balance_guidelines=["favor whole foods"],
            foods_to_emphasize=["vegetables"],
            foods_to_avoid=["ultra-processed foods"],
            notes="general guidance only",
            clinician_note=(
                default_clinician_note or "Please work with your clinician or registered dietitian."
            ),
        )


def _build_orchestrator(narrator: _FakeNutritionist) -> "object":
    """Build an orchestrator with fake narrator and disabled stores."""
    from nutrition_meal_planning_team.orchestrator.agent import (
        NutritionMealPlanningOrchestrator,
    )

    # Import lazily so hitting this module doesn't drag in strands.
    orch = object.__new__(NutritionMealPlanningOrchestrator)
    orch.profile_store = None  # not used by _build_nutrition_plan
    orch.meal_feedback_store = None
    orch.nutrition_plan_store = None
    orch.intake_agent = None
    orch.nutritionist_agent = narrator
    orch.meal_planning_agent = None
    orch.chat_agent = None
    return orch


def _profile(**bio_overrides):
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


# --- Happy path ----------------------------------------------------------


def test_build_plan_assembles_targets_plus_narrative():
    orch = _build_orchestrator(_FakeNutritionist())
    plan = orch._build_nutrition_plan(_profile())

    # Numeric targets from the calculator.
    assert plan.daily_targets.calories_kcal is not None
    assert plan.daily_targets.calories_kcal > 0
    # Narrative from our fake.
    assert plan.balance_guidelines == ["eat more veg"]
    assert plan.foods_to_emphasize == ["leafy greens"]
    # Metadata.
    assert plan.cohort == PlanCohort.general_adult
    assert plan.is_guidance_only is False
    assert plan.calculator_version == CALCULATOR_VERSION
    assert "bmr_kcal" in plan.intermediates
    assert "tdee_kcal" in plan.intermediates
    assert plan.rationale is not None
    assert "steps" in plan.rationale


# --- Cohort branches -----------------------------------------------------


def test_minor_routes_to_guidance_only():
    orch = _build_orchestrator(_FakeNutritionist())
    plan = orch._build_nutrition_plan(_profile(age_years=15))
    assert plan.is_guidance_only is True
    assert plan.cohort == PlanCohort.minor
    # No numeric kcal target.
    assert plan.daily_targets.calories_kcal in (None, 0)
    # Clinician note present.
    assert plan.clinician_note
    assert plan.rationale is None  # no rationale on guidance-only


def test_ckd_stage_5_routes_to_clinician_guided():
    p = _profile()
    p.clinical = ClinicalInfo(conditions=["ckd_stage_5"])
    orch = _build_orchestrator(_FakeNutritionist())
    plan = orch._build_nutrition_plan(p)
    assert plan.is_guidance_only is True
    assert plan.cohort == PlanCohort.clinician_guided
    assert plan.clinician_note


def test_pregnancy_t2_stays_in_numeric_path():
    """Pregnancy cohort still gets numeric targets (+340 kcal, not a deficit)."""
    p = _profile()
    p.clinical = ClinicalInfo(reproductive_state=ReproductiveState.pregnant_t2)
    orch = _build_orchestrator(_FakeNutritionist())
    plan = orch._build_nutrition_plan(p)
    assert plan.is_guidance_only is False
    assert plan.cohort == PlanCohort.pregnancy_lactation
    assert plan.daily_targets.calories_kcal > 0


def test_ed_history_flag_stays_in_numeric_path_but_no_deficit():
    p = _profile()
    p.clinical = ClinicalInfo(ed_history_flag=True)
    p.goals = GoalsInfo(goal_type="lose_weight", rate_kg_per_week=0.5)
    orch = _build_orchestrator(_FakeNutritionist())
    plan = orch._build_nutrition_plan(p)
    assert plan.cohort == PlanCohort.ed_adjacent
    assert plan.is_guidance_only is False
    # kcal target must equal TDEE, not a deficit.
    assert plan.daily_targets.calories_kcal == pytest.approx(
        plan.intermediates["tdee_kcal"], abs=1.0
    )


# --- Insufficient input --------------------------------------------------


def test_missing_biometrics_returns_guidance_only_with_missing_fields():
    p = _profile()
    p.biometrics.weight_kg = None
    orch = _build_orchestrator(_FakeNutritionist())
    plan = orch._build_nutrition_plan(p)
    assert plan.is_guidance_only is True
    assert "missing_fields" in plan.metadata
    assert "weight_kg" in plan.metadata["missing_fields"]
    # Notes field spells out what's missing for the UI.
    assert "weight_kg" in plan.notes
    # No LLM call was made, so narrative lists are empty.
    assert plan.balance_guidelines == []


# --- Safety invariants ---------------------------------------------------


def test_safety_invariant_raises_on_minor_with_numeric_plan(monkeypatch):
    """Simulate a calculator bug: route a minor past the cohort check and
    produce a numeric plan. The orchestrator's belt-and-suspenders
    rail must catch it."""
    from nutrition_meal_planning_team.orchestrator.agent import (
        SafetyInvariantError,
    )

    orch = _build_orchestrator(_FakeNutritionist())
    # Build a valid plan for a general adult, then pretend its owner
    # is a minor by editing the profile before the invariant check.
    # Easier: call _assert_safety_invariants directly with mismatched args.
    good_profile = _profile()
    numeric_plan = orch._build_nutrition_plan(good_profile)
    minor_profile = _profile(age_years=14)
    with pytest.raises(SafetyInvariantError) as exc_info:
        orch._assert_safety_invariants(minor_profile, numeric_plan)
    assert "minor" in str(exc_info.value).lower()


def test_safety_invariant_raises_on_ed_with_deficit(monkeypatch):
    from nutrition_meal_planning_team.orchestrator.agent import SafetyInvariantError

    orch = _build_orchestrator(_FakeNutritionist())
    # Construct a plan where kcal < tdee (simulating a deficit-bearing plan
    # for an ED profile — calculator would never produce this, but the
    # invariant should catch it if something upstream did).
    from nutrition_meal_planning_team.models import DailyTargets, NutritionPlan

    bad_plan = NutritionPlan(
        daily_targets=DailyTargets(calories_kcal=1600),
        intermediates={"tdee_kcal": 2100, "bmr_kcal": 1400},
        is_guidance_only=False,
    )
    p = _profile()
    p.clinical = ClinicalInfo(ed_history_flag=True)
    with pytest.raises(SafetyInvariantError) as exc_info:
        orch._assert_safety_invariants(p, bad_plan)
    assert "ED" in str(exc_info.value) or "deficit" in str(exc_info.value)


def test_safety_invariant_kcal_floor():
    from nutrition_meal_planning_team.models import DailyTargets, NutritionPlan
    from nutrition_meal_planning_team.orchestrator.agent import SafetyInvariantError

    orch = _build_orchestrator(_FakeNutritionist())
    bad_plan = NutritionPlan(
        daily_targets=DailyTargets(calories_kcal=1100),  # below 1200 floor
        is_guidance_only=False,
    )
    with pytest.raises(SafetyInvariantError):
        orch._assert_safety_invariants(_profile(), bad_plan)


def test_safety_invariant_passes_on_well_formed_plan():
    orch = _build_orchestrator(_FakeNutritionist())
    plan = orch._build_nutrition_plan(_profile())
    # Re-assert on the built plan — should be a no-op.
    assert orch._assert_safety_invariants(_profile(), plan) is plan
