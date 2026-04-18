"""Orchestrator: routes profile/plan/meals/feedback/chat to the right agents and stores."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from llm_service import get_strands_model

from ..agents.chat_agent import NutritionChatAgent
from ..agents.intake_profile_agent import IntakeProfileAgent
from ..agents.meal_planning_agent import MealPlanningAgent
from ..agents.nutritionist_agent import NutritionistAgent
from ..clinical_taxonomy import (
    parse_conditions as _parse_conditions,
)
from ..clinical_taxonomy import (
    parse_medications as _parse_medications,
)
from ..models import (
    BiometricHistoryResponse,
    BiometricPatchRequest,
    ChatRequest,
    ChatResponse,
    ClientProfile,
    ClinicalInfo,
    ClinicalPatchRequest,
    ClinicianOverrideRequest,
    CompletenessResponse,
    DailyTargets,
    FeedbackRequest,
    FeedbackResponse,
    MealHistoryResponse,
    MealPlanRequest,
    MealPlanResponse,
    MealRecommendationWithId,
    NutritionPlan,
    NutritionPlanRequest,
    NutritionPlanResponse,
    PlanCohort,
    ProfileUpdateRequest,
    ReproductiveState,
)
from ..nutrition_calc import (
    CALCULATOR_VERSION,
    InsufficientInputError,
    UnsupportedCohortError,
    compute_daily_targets,
)
from ..shared.client_profile_store import ClientProfileStore, get_profile_store
from ..shared.meal_feedback_store import MealFeedbackStore, get_meal_feedback_store
from ..shared.nutrition_plan_store import NutritionPlanStore, get_nutrition_plan_store
from ..units import coerce_height_cm, coerce_weight_kg


class SafetyInvariantError(RuntimeError):
    """Raised when the orchestrator's belt-and-suspenders check catches a
    calculator bug that produced an unsafe plan (SPEC-004 §4.8).

    Steady-state this must remain zero. A non-zero rate in production
    is a pageable incident — the calculator is supposed to have
    prevented the violation already.
    """


logger = logging.getLogger(__name__)


class NutritionMealPlanningOrchestrator:
    """
    Single entry point: load profile/history as needed, delegate to intake/nutritionist/meal_planning agents.
    All API routes should delegate to this class.
    """

    def __init__(
        self,
        profile_store: Optional[ClientProfileStore] = None,
        meal_feedback_store: Optional[MealFeedbackStore] = None,
        nutrition_plan_store: Optional[NutritionPlanStore] = None,
        llm_model: Optional[Any] = None,
    ) -> None:
        self.profile_store = profile_store or get_profile_store()
        self.meal_feedback_store = meal_feedback_store or get_meal_feedback_store()
        self.nutrition_plan_store = nutrition_plan_store or get_nutrition_plan_store()
        model = llm_model or get_strands_model("nutrition_meal_planning")
        self.intake_agent = IntakeProfileAgent(model)
        # SPEC-004: NutritionistAgent is a narrator on top of the
        # calculator; it pulls its own llm_service client lazily and
        # does not take a strands model.
        self.nutritionist_agent = NutritionistAgent()
        self.meal_planning_agent = MealPlanningAgent(model)
        self.chat_agent = NutritionChatAgent(
            model, self.intake_agent, self.nutritionist_agent, self.meal_planning_agent
        )

    def get_profile(self, client_id: str) -> Optional[ClientProfile]:
        """Get client profile or None if not found."""
        return self.profile_store.get_profile(client_id)

    def update_profile(self, client_id: str, update: ProfileUpdateRequest) -> ClientProfile:
        """Validate/complete profile with intake agent and save."""
        current = self.profile_store.get_profile(client_id)
        if current is None:
            current = self.profile_store.create_profile(client_id)
        profile = self.intake_agent.run(client_id, update=update, current_profile=current)
        self.profile_store.save_profile(client_id, profile)
        return profile

    def _get_or_generate_nutrition_plan(self, profile: ClientProfile) -> NutritionPlan:
        """Return cached nutrition plan if inputs unchanged, otherwise rebuild.

        SPEC-004 §4.4. Cache key includes ``CALCULATOR_VERSION`` so a
        calculator upgrade invalidates every client's plan
        deterministically on first read post-deploy.
        """
        cached = self.nutrition_plan_store.get_cached_plan(
            profile.client_id, profile, calculator_version=CALCULATOR_VERSION
        )
        if cached is not None:
            return cached
        plan = self._build_nutrition_plan(profile)
        self.nutrition_plan_store.save_plan(
            profile.client_id, profile, plan, calculator_version=CALCULATOR_VERSION
        )
        return plan

    def _build_nutrition_plan(self, profile: ClientProfile) -> NutritionPlan:
        """Deterministic calculator → LLM narrator → assembled NutritionPlan.

        SPEC-004 §4.4. Returns a ``NutritionPlan`` with numeric
        targets from ``nutrition_calc`` and narrative from the
        refactored ``NutritionistAgent``. On unsupported cohorts
        (minor / CKD 4-5) or missing biometrics, emits a
        guidance-only plan.
        """
        try:
            calc = compute_daily_targets(profile)
        except UnsupportedCohortError as exc:
            plan = self._build_guidance_only_plan(
                profile,
                cohort=PlanCohort(exc.cohort),
                guidance_key=exc.guidance_key,
                clinician_note=exc.clinician_note,
            )
            return self._assert_safety_invariants(profile, plan)
        except InsufficientInputError as exc:
            plan = self._build_insufficient_input_plan(profile, exc)
            return self._assert_safety_invariants(profile, plan)

        narrative = self.nutritionist_agent.narrate_plan(
            profile, calc.targets, self._rationale_to_dict(calc.rationale)
        )
        plan = NutritionPlan(
            daily_targets=calc.targets,
            balance_guidelines=list(narrative.balance_guidelines),
            foods_to_emphasize=list(narrative.foods_to_emphasize),
            foods_to_avoid=list(narrative.foods_to_avoid),
            notes=narrative.notes,
            rationale=self._rationale_to_dict(calc.rationale),
            calculator_version=calc.calculator_version,
            cohort=PlanCohort(calc.cohort),
            is_guidance_only=False,
            clinician_note=None,
            intermediates=dict(calc.intermediates or {}),
            metadata=dict(calc.metadata or {}),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        return self._assert_safety_invariants(profile, plan)

    def _build_guidance_only_plan(
        self,
        profile: ClientProfile,
        *,
        cohort: PlanCohort,
        guidance_key: str,
        clinician_note: str,
    ) -> NutritionPlan:
        """Guidance-only plan for cohorts that refuse numeric targets."""
        payload = self.nutritionist_agent.narrate_general_guidance(
            profile, guidance_key, default_clinician_note=clinician_note
        )
        return NutritionPlan(
            daily_targets=DailyTargets(),
            balance_guidelines=list(payload.balance_guidelines),
            foods_to_emphasize=list(payload.foods_to_emphasize),
            foods_to_avoid=list(payload.foods_to_avoid),
            notes=payload.notes,
            rationale=None,
            calculator_version=CALCULATOR_VERSION,
            cohort=cohort,
            is_guidance_only=True,
            clinician_note=payload.clinician_note or clinician_note,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def _build_insufficient_input_plan(
        self, profile: ClientProfile, exc: InsufficientInputError
    ) -> NutritionPlan:
        """Plan emitted when required biometrics are missing.

        We do not call the LLM on this path — there is nothing useful
        to narrate. The response's ``notes`` field names the missing
        fields so the UI can deep-link the user to the right input.
        """
        fields = ", ".join(exc.fields) if exc.fields else "biometric inputs"
        note = (
            f"We need {fields} before we can compute personalized "
            "targets. Update your profile to unlock a full plan."
        )
        return NutritionPlan(
            daily_targets=DailyTargets(),
            balance_guidelines=[],
            foods_to_emphasize=[],
            foods_to_avoid=[],
            notes=note,
            rationale=None,
            calculator_version=CALCULATOR_VERSION,
            cohort=PlanCohort.general_adult,
            is_guidance_only=True,
            clinician_note=None,
            metadata={"missing_fields": list(exc.fields)},
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def _assert_safety_invariants(
        self, profile: ClientProfile, plan: NutritionPlan
    ) -> NutritionPlan:
        """Belt-and-suspenders re-assertion of the SPEC-003 rails.

        This should never fire: the calculator's cohort router and
        safety floor already enforce each invariant. Any violation
        here means a calculator bug made it past testing; we refuse
        to emit the plan and surface a SafetyInvariantError so the
        caller can produce a graceful error and the metric pages.
        """
        bio = profile.biometrics
        clin = profile.clinical
        # Minor: numeric targets are not allowed.
        if bio.age_years is not None and bio.age_years < 18:
            if not plan.is_guidance_only:
                raise SafetyInvariantError("minor profile produced numeric-target plan")
            if profile.goals and profile.goals.goal_type == "lose_weight":
                # Still guidance_only — do not throw; just scrub the goal
                # type from the plan's view. The profile itself is
                # untouched (that's the profile API's concern).
                pass

        # ED-history flag: no deficit may be applied.
        if clin and clin.ed_history_flag and not plan.is_guidance_only:
            # The calculator's ed_adjacent cohort sets kcal = TDEE.
            # We re-assert: kcal_target must be >= intermediates.tdee_kcal
            # (no deficit). Guidance-only plans have no numeric kcal so
            # they are exempt.
            kcal = plan.daily_targets.calories_kcal
            tdee = plan.intermediates.get("tdee_kcal")
            if kcal is not None and tdee is not None and kcal < tdee - 1:
                raise SafetyInvariantError("ED-history profile produced deficit-bearing plan")

        # kcal floor: calculator guarantees >=1200; cross-check here.
        kcal = plan.daily_targets.calories_kcal
        if kcal is not None and kcal > 0 and kcal < 1200:
            raise SafetyInvariantError(f"plan kcal {kcal} is below the 1200 safety floor")
        return plan

    @staticmethod
    def _rationale_to_dict(rationale) -> dict | None:
        """Serialize the nutrition_calc Rationale dataclass for JSONB storage."""
        if rationale is None:
            return None
        return {
            "cohort": rationale.cohort,
            "applied_overrides": list(rationale.applied_overrides),
            "steps": [
                {
                    "id": step.id,
                    "label": step.label,
                    "inputs": step.inputs,
                    "outputs": step.outputs,
                    "source": step.source,
                    "note": step.note,
                }
                for step in rationale.steps
            ],
        }

    def regenerate_nutrition_plan(self, client_id: str) -> NutritionPlanResponse:
        """Force a cache miss and rebuild the plan (SPEC-004 §4.7)."""
        profile = self.profile_store.get_profile(client_id)
        if profile is None:
            raise ValueError("Profile not found")
        self.nutrition_plan_store.invalidate_plan(client_id)
        plan = self._get_or_generate_nutrition_plan(profile)
        return NutritionPlanResponse(client_id=client_id, plan=plan)

    def get_rationale(self, client_id: str) -> dict | None:
        """Return the rationale + intermediates for the latest cached plan
        (SPEC-004 §4.7). None if no plan exists yet."""
        profile = self.profile_store.get_profile(client_id)
        if profile is None:
            return None
        plan = self._get_or_generate_nutrition_plan(profile)
        return {
            "client_id": client_id,
            "rationale": plan.rationale,
            "intermediates": plan.intermediates,
            "cohort": plan.cohort.value if plan.cohort else None,
            "calculator_version": plan.calculator_version,
            "is_guidance_only": plan.is_guidance_only,
        }

    def get_nutrition_plan(self, request: NutritionPlanRequest) -> NutritionPlanResponse:
        """Load profile, run nutritionist agent, return plan."""
        profile = self.profile_store.get_profile(request.client_id)
        if profile is None:
            raise ValueError("Profile not found")
        plan = self._get_or_generate_nutrition_plan(profile)
        return NutritionPlanResponse(client_id=request.client_id, plan=plan)

    def get_meal_plan(self, request: MealPlanRequest) -> MealPlanResponse:
        """Load profile, nutrition plan, meal history; run meal planning agent; record each suggestion."""
        profile = self.profile_store.get_profile(request.client_id)
        if profile is None:
            raise ValueError("Profile not found")
        nutrition_plan = self._get_or_generate_nutrition_plan(profile)
        meal_history = self.meal_feedback_store.get_meal_history(request.client_id, limit=50)
        suggestions = self.meal_planning_agent.run(
            profile,
            nutrition_plan,
            meal_history,
            period_days=request.period_days,
            meal_types=request.meal_types,
        )
        with_ids = self._record_suggestions(request.client_id, suggestions)
        return MealPlanResponse(client_id=request.client_id, suggestions=with_ids)

    def submit_feedback(self, request: FeedbackRequest) -> FeedbackResponse:
        """Record feedback for a recommendation."""
        ok = self.meal_feedback_store.record_feedback(
            request.recommendation_id,
            rating=request.rating,
            would_make_again=request.would_make_again,
            notes=request.notes,
        )
        return FeedbackResponse(recommendation_id=request.recommendation_id, recorded=ok)

    def get_meal_history(self, client_id: str, limit: int = 100) -> MealHistoryResponse:
        """Return past recommendations and feedback for the client."""
        entries = self.meal_feedback_store.get_meal_history(client_id, limit=limit)
        return MealHistoryResponse(client_id=client_id, entries=entries)

    # --- SPEC-002: biometric + clinical patch paths ---

    def patch_biometrics(
        self,
        client_id: str,
        patch: BiometricPatchRequest,
        recorded_by: Optional[str] = None,
    ) -> ClientProfile:
        """Apply a biometric patch, log every changed field, save profile.

        Canonical units (cm, kg) win when both sides are provided.
        Entries not present in the patch leave the existing value
        untouched. The audit log row captures the canonical (stored)
        value and its unit, not whatever the user typed.
        """
        profile = self.profile_store.get_profile(client_id)
        if profile is None:
            profile = self.profile_store.create_profile(client_id)

        bio = profile.biometrics
        changes: list[tuple[str, Optional[float], Optional[str], Optional[str]]] = []

        if patch.sex is not None and patch.sex != bio.sex:
            bio.sex = patch.sex
            changes.append(("sex", None, bio.sex.value, None))

        if patch.age_years is not None and patch.age_years != bio.age_years:
            bio.age_years = patch.age_years
            changes.append(("age_years", float(patch.age_years), None, "years"))

        # Height: coerce ft/in or cm into canonical cm.
        new_height = coerce_height_cm(
            height_cm=patch.height_cm,
            height_ft=patch.height_ft,
            height_in=patch.height_in,
        )
        if new_height is not None and new_height != bio.height_cm:
            bio.height_cm = new_height
            changes.append(("height_cm", new_height, None, "cm"))

        # Weight: coerce lb or kg into canonical kg.
        new_weight = coerce_weight_kg(
            weight_kg=patch.weight_kg,
            weight_lb=patch.weight_lb,
        )
        if new_weight is not None and new_weight != bio.weight_kg:
            bio.weight_kg = new_weight
            changes.append(("weight_kg", new_weight, None, "kg"))

        if patch.body_fat_pct is not None and patch.body_fat_pct != bio.body_fat_pct:
            bio.body_fat_pct = patch.body_fat_pct
            changes.append(("body_fat_pct", float(patch.body_fat_pct), None, "pct"))

        if patch.activity_level is not None and patch.activity_level != bio.activity_level:
            bio.activity_level = patch.activity_level
            changes.append(("activity_level", None, bio.activity_level.value, None))

        if patch.timezone is not None and patch.timezone != bio.timezone:
            bio.timezone = patch.timezone
            changes.append(("timezone", None, bio.timezone, None))

        if patch.preferred_units is not None and patch.preferred_units != bio.preferred_units:
            bio.preferred_units = patch.preferred_units
            changes.append(("preferred_units", None, bio.preferred_units, None))

        measured_at_changed = False
        if patch.measured_at is not None and patch.measured_at != bio.measured_at:
            bio.measured_at = patch.measured_at
            measured_at_changed = True
            # measured_at is metadata, not a numeric value — no audit row.

        if changes or measured_at_changed:
            self.profile_store.save_profile(client_id, profile)
            for field, vnum, vtext, unit in changes:
                self.profile_store.log_biometric(
                    client_id,
                    field,
                    value_numeric=vnum,
                    value_text=vtext,
                    unit=unit,
                    source="manual",
                    recorded_by=recorded_by,
                )
        return profile

    def get_biometric_history(
        self,
        client_id: str,
        field: Optional[str] = None,
        since_iso: Optional[str] = None,
        limit: int = 200,
    ) -> BiometricHistoryResponse:
        """Return biometric log rows for the client, newest first."""
        since_dt: Optional[datetime] = None
        if since_iso:
            # ``datetime.fromisoformat`` on Python 3.10 rejects the
            # trailing ``Z`` shorthand common in RFC3339. Normalize
            # it to ``+00:00`` so callers can pass standard API
            # timestamps without having to pre-process them.
            normalized = since_iso[:-1] + "+00:00" if since_iso.endswith("Z") else since_iso
            try:
                since_dt = datetime.fromisoformat(normalized)
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                since_dt = None
        entries = self.profile_store.get_biometric_history(
            client_id, field=field, since=since_dt, limit=limit
        )
        return BiometricHistoryResponse(client_id=client_id, field=field, entries=entries)

    def patch_clinical(
        self,
        client_id: str,
        patch: ClinicalPatchRequest,
    ) -> ClientProfile:
        """Apply a clinical patch with whole-list replace semantics.

        Recognized conditions / medications are stored on the enum-
        backed lists; unrecognized strings land in ``_freetext`` and
        are surfaced to the agent narrator but never drive clamps.
        """
        profile = self.profile_store.get_profile(client_id)
        if profile is None:
            profile = self.profile_store.create_profile(client_id)

        if profile.clinical is None:
            profile.clinical = ClinicalInfo()

        if patch.conditions is not None:
            known, unknown = _parse_conditions(patch.conditions)
            profile.clinical.conditions = [c.value for c in known]
            profile.clinical.conditions_freetext = unknown

        if patch.medications is not None:
            known_meds, unknown_meds = _parse_medications(patch.medications)
            profile.clinical.medications = [m.value for m in known_meds]
            profile.clinical.medications_freetext = unknown_meds

        if patch.reproductive_state is not None:
            profile.clinical.reproductive_state = patch.reproductive_state

        if patch.ed_history_flag is not None:
            profile.clinical.ed_history_flag = patch.ed_history_flag

        self.profile_store.save_profile(client_id, profile)
        return profile

    def put_clinician_overrides(
        self,
        client_id: str,
        req: ClinicianOverrideRequest,
    ) -> ClientProfile:
        """Replace the clinician-overrides dict wholesale; audit every key.

        Admin-only caller (the API enforces auth; this method trusts the
        caller has already verified it). Each changed key produces one
        audit row with ``author`` and optional ``reason``.
        """
        profile = self.profile_store.get_profile(client_id)
        if profile is None:
            profile = self.profile_store.create_profile(client_id)

        if profile.clinical is None:
            profile.clinical = ClinicalInfo()

        old = dict(profile.clinical.clinician_overrides or {})
        new = dict(req.overrides or {})

        changed_keys = set(old.keys()) ^ set(new.keys())
        for key in set(old.keys()) & set(new.keys()):
            if old[key] != new[key]:
                changed_keys.add(key)

        profile.clinical.clinician_overrides = new
        self.profile_store.save_profile(client_id, profile)

        for key in changed_keys:
            self.profile_store.log_clinical_override(
                client_id,
                key,
                new.get(key),
                author=req.author or "admin",
                reason=req.reason,
            )
        return profile

    def get_completeness(self, client_id: str) -> CompletenessResponse:
        """Compute which calculator-driving fields are populated.

        This drives the UI banner and the SPEC-004 agent's decision to
        return guidance-only vs. numeric targets. We do not throw 404
        for an unknown client — an empty profile is a valid input.
        """
        profile = self.profile_store.get_profile(client_id)
        resp = CompletenessResponse(client_id=client_id)
        if profile is None:
            resp.blockers = [
                "no_profile",
                "missing_sex",
                "missing_age_years",
                "missing_height_cm",
                "missing_weight_kg",
            ]
            return resp

        bio = profile.biometrics
        blockers: list[str] = []

        # Biometrics required for numeric targets.
        from ..models import Sex as _Sex

        if bio.sex == _Sex.unspecified:
            blockers.append("missing_sex")
        if bio.age_years is None:
            blockers.append("missing_age_years")
        if bio.height_cm is None:
            blockers.append("missing_height_cm")
        if bio.weight_kg is None:
            blockers.append("missing_weight_kg")

        resp.has_biometrics = not blockers  # all four required present
        resp.has_activity = bio.activity_level is not None

        # Clinical context "confirmed" when the user has saved any
        # clinical state at all (a deliberate yes/no/none). Empty
        # conditions+medications+reproductive_state=none is still a
        # confirmed state; we treat it as such if the user has
        # touched the clinical section. v1 proxy: if ed_history_flag
        # has been explicitly set OR any clinical list is non-empty
        # OR reproductive_state != none, we consider it confirmed.
        c = profile.clinical
        resp.has_clinical_confirmed = bool(
            c
            and (
                c.conditions
                or c.conditions_freetext
                or c.medications
                or c.medications_freetext
                or c.reproductive_state != ReproductiveState.none
                or c.ed_history_flag
            )
        )

        resp.is_minor = bio.age_years is not None and bio.age_years < 18
        if resp.is_minor:
            blockers.append("minor_guidance_only")

        resp.ed_history_flag = bool(c and c.ed_history_flag)

        resp.blockers = blockers
        return resp

    # --- Chat ---

    def handle_chat(self, body: ChatRequest) -> ChatResponse:
        """Process one chat turn: run chat agent, then execute any triggered action."""
        client_id = body.client_id.strip()
        profile = self.profile_store.get_profile(client_id)

        # Only load nutrition plan and history when profile exists and has meaningful data
        nutrition_plan: Optional[NutritionPlan] = None
        meal_history: List = []
        if profile is not None:
            try:
                nutrition_plan = self._get_or_generate_nutrition_plan(profile)
            except Exception:
                nutrition_plan = None
            meal_history = self.meal_feedback_store.get_meal_history(client_id, limit=50)

        history_dicts = [{"role": m.role, "content": m.content} for m in body.conversation_history]

        result = self.chat_agent.run(
            client_id=client_id,
            user_message=body.message,
            conversation_history=history_dicts,
            profile=profile,
            nutrition_plan=nutrition_plan,
            meal_suggestions=None,
            meal_history=meal_history or None,
        )

        action = result.get("action", "none")
        response = ChatResponse(
            message=result.get("message", ""),
            phase=result.get("phase", "intake"),
            action=action,
        )

        if action == "save_profile":
            response.profile = self._handle_save_profile(client_id, result, profile)
        elif action == "generate_nutrition_plan":
            response.nutrition_plan = self._handle_generate_nutrition_plan(client_id, profile)
        elif action == "generate_meals":
            response.meal_suggestions = self._handle_generate_meals(client_id, result, profile)
        elif action == "submit_feedback":
            response.feedback_recorded = self._handle_submit_feedback(result, meal_history)

        return response

    # --- Private helpers ---

    def _record_suggestions(
        self, client_id: str, suggestions: list
    ) -> list[MealRecommendationWithId]:
        """Record each suggestion in the store and attach recommendation IDs."""
        with_ids: list[MealRecommendationWithId] = []
        for s in suggestions:
            rec_id = self.meal_feedback_store.record_recommendation(client_id, s.model_dump())
            with_ids.append(MealRecommendationWithId(**s.model_dump(), recommendation_id=rec_id))
        return with_ids

    def _handle_save_profile(
        self, client_id: str, result: Dict[str, Any], profile: Optional[ClientProfile]
    ) -> Optional[ClientProfile]:
        extracted = result.get("extracted_profile") or {}
        update_data: dict = {}
        for key in (
            "household",
            "dietary_needs",
            "allergies_and_intolerances",
            "lifestyle",
            "preferences",
            "goals",
        ):
            if key in extracted:
                update_data[key] = extracted[key]

        if update_data:
            update_req = ProfileUpdateRequest.model_validate(update_data)
            current = self.profile_store.get_profile(client_id)
            if current is None:
                current = self.profile_store.create_profile(client_id)
            saved_profile = self.intake_agent.run(
                client_id, update=update_req, current_profile=current
            )
            self.profile_store.save_profile(client_id, saved_profile)
            return saved_profile
        return profile

    def _handle_generate_nutrition_plan(
        self, client_id: str, profile: Optional[ClientProfile]
    ) -> Optional[NutritionPlan]:
        """Chat-initiated plan generation (SPEC-004 parity).

        Routes through the same ``_get_or_generate_nutrition_plan``
        path the direct API uses, so a chat request and a direct
        ``POST /plan/nutrition`` for the same profile return byte-
        equal plans (SPEC-004 §6.2 parity test).
        """
        p = profile or self.profile_store.get_profile(client_id)
        if p:
            try:
                return self._get_or_generate_nutrition_plan(p)
            except Exception as e:
                logger.warning("Nutrition plan generation failed during chat: %s", e)
        return None

    def _handle_generate_meals(
        self, client_id: str, result: Dict[str, Any], profile: Optional[ClientProfile]
    ) -> list[MealRecommendationWithId]:
        p = profile or self.profile_store.get_profile(client_id)
        if not p:
            return []
        try:
            params = result.get("meal_plan_params") or {}
            period_days = params.get("period_days", 7)
            meal_types = params.get("meal_types", ["lunch", "dinner"])
            np = self._get_or_generate_nutrition_plan(p)
            mh = self.meal_feedback_store.get_meal_history(client_id, limit=50)
            suggestions = self.meal_planning_agent.run(
                p, np, mh, period_days=period_days, meal_types=meal_types
            )
            return self._record_suggestions(client_id, suggestions)
        except Exception as e:
            logger.warning("Meal plan generation failed during chat: %s", e)
            return []

    def _handle_submit_feedback(self, result: Dict[str, Any], meal_history: list) -> bool:
        fb = result.get("feedback_data") or {}
        meal_name = fb.get("meal_name", "").strip().lower()
        rating = fb.get("rating")
        would_make_again = fb.get("would_make_again")
        notes = fb.get("notes", "")

        if meal_name and meal_history:
            for entry in meal_history:
                snap = entry.meal_snapshot or {}
                name = (snap.get("name") or "").strip().lower()
                if name and (meal_name in name or name in meal_name):
                    self.meal_feedback_store.record_feedback(
                        entry.recommendation_id,
                        rating=rating,
                        would_make_again=would_make_again,
                        notes=notes,
                    )
                    return True
        return False
