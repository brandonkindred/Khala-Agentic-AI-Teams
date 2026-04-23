"""Pydantic models for Nutrition & Meal Planning team."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field, field_validator

# SPEC-006: import only the taxonomy enums. ``ingredient_kb.taxonomy`` is a
# pure stdlib module, so this is cycle-safe. Never import from
# ``ingredient_kb.catalog`` or ``parser`` here — those pull yaml + file I/O.
from .ingredient_kb.taxonomy import AllergenTag, DietaryTag

# SPEC-002 schema version. Bump when the ClientProfile shape changes
# in a way downstream consumers must observe. Kept as a plain string
# so JSON round-trips via Postgres JSONB are lossless.
PROFILE_SCHEMA_VERSION = "2.0"

# --- Client profile (what a dietician would need) ---


class HouseholdMember(BaseModel):
    """One person in the household (for tailored planning)."""

    name: str = ""
    age_or_role: str = ""  # e.g. "adult", "child 8", "teen"
    dietary_needs: List[str] = Field(default_factory=list)
    allergies: List[str] = Field(default_factory=list)
    notes: str = ""


class HouseholdInfo(BaseModel):
    """Who is being fed."""

    number_of_people: int = 1
    description: str = ""  # e.g. "solo", "couple", "family of 4"
    ages_if_relevant: List[str] = Field(default_factory=list)  # e.g. ["adult", "child 8"]
    members: List[HouseholdMember] = Field(default_factory=list)


class LifestyleInfo(BaseModel):
    """Lifestyle and busyness constraints."""

    max_cooking_time_minutes: Optional[int] = None  # e.g. 15, 30
    lunch_context: str = "remote"  # "office" (portable, minimal prep) or "remote" (can cook/reheat)
    equipment_constraints: List[str] = Field(
        default_factory=list
    )  # e.g. "no oven", "limited equipment"
    other_constraints: str = ""


class PreferencesInfo(BaseModel):
    """Food and drink preferences."""

    cuisines_liked: List[str] = Field(default_factory=list)
    cuisines_disliked: List[str] = Field(default_factory=list)
    ingredients_disliked: List[str] = Field(default_factory=list)
    preferences_free_text: str = ""  # "prefer X over Y", etc.


# --- Biometrics (SPEC-002) -----------------------------------------------


class Sex(str, Enum):
    """Biological-sex options used by the calculator (SPEC-003).

    ``other`` and ``unspecified`` route to a sex-averaged BMR variant.
    """

    female = "female"
    male = "male"
    other = "other"
    unspecified = "unspecified"


class ActivityLevel(str, Enum):
    """Activity categories with PAL multipliers defined by SPEC-003.

    sedentary=1.2, light=1.375, moderate=1.55, active=1.725,
    very_active=1.9. The multipliers themselves live in ``nutrition_calc``
    (SPEC-003); this enum only names the categories.
    """

    sedentary = "sedentary"
    light = "light"
    moderate = "moderate"
    active = "active"
    very_active = "very_active"


class BiometricInfo(BaseModel):
    """Inputs the calculator needs.

    All numeric fields have implausibility bounds at the Pydantic layer.
    Outside-range values are **rejected**, not clamped; the API returns
    422 so the client can surface a clear error. See SPEC-002 §4.1.
    """

    sex: Sex = Sex.unspecified
    age_years: Optional[int] = Field(default=None, ge=2, le=120)
    height_cm: Optional[float] = Field(default=None, ge=50, le=260)
    weight_kg: Optional[float] = Field(default=None, ge=20, le=400)
    body_fat_pct: Optional[float] = Field(default=None, ge=3, le=75)
    activity_level: ActivityLevel = ActivityLevel.sedentary
    timezone: str = "UTC"
    measured_at: Optional[str] = None
    # User's preferred display units for UI round-trips. Not used by any
    # calculator input; purely cosmetic.
    preferred_units: str = "metric"  # 'metric' | 'imperial'

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        if not v:
            return "UTC"
        # Lazy import so we don't crash on platforms without tzdata.
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {v}") from exc
        except Exception:
            # Missing tzdata on the host — accept and let the user
            # resolve it rather than failing the whole profile write.
            return v
        return v

    @field_validator("preferred_units")
    @classmethod
    def _validate_units(cls, v: str) -> str:
        if v not in ("metric", "imperial"):
            raise ValueError("preferred_units must be 'metric' or 'imperial'")
        return v


# --- Clinical (SPEC-002) -------------------------------------------------


class ReproductiveState(str, Enum):
    """Pregnancy / lactation stage for cohort routing.

    SPEC-003's cohort router skips deficit calculation for pregnancy
    and lactation and applies trimester-specific kcal additions.
    """

    none = "none"
    pregnant_t1 = "pregnant_t1"
    pregnant_t2 = "pregnant_t2"
    pregnant_t3 = "pregnant_t3"
    lactating = "lactating"
    postpartum = "postpartum"


class ClinicalInfo(BaseModel):
    """Medical context driving SPEC-003 clinical overrides and SPEC-007
    medication-interaction checks.

    ``conditions`` / ``medications`` hold the **recognized** entries from
    ``clinical_taxonomy.Condition`` / ``Medication``. Anything the user
    enters that isn't in those enums lives in the ``*_freetext`` lists —
    still surfaced to the narrator, never used to drive numeric clamps.
    """

    conditions: List[str] = Field(default_factory=list)
    conditions_freetext: List[str] = Field(default_factory=list)
    medications: List[str] = Field(default_factory=list)
    medications_freetext: List[str] = Field(default_factory=list)
    reproductive_state: ReproductiveState = ReproductiveState.none
    # ED history flag: when True, scale-centric UX is disabled and
    # SPEC-003 refuses deficit goals regardless of goal_type. This is
    # a team invariant, not a per-user toggle — see SPEC-002 §4.1.
    ed_history_flag: bool = False
    # Clinician-authored overrides (e.g. ``{"bmi_floor": 19.5}``).
    # Admin-only write path; users cannot edit these.
    clinician_overrides: Dict[str, float] = Field(default_factory=dict)


class GoalsInfo(BaseModel):
    """Nutrition goals (optional).

    ``goal_type`` is the high-level intent; ``target_weight_kg`` and
    ``rate_kg_per_week`` parameterize the calculator's energy delta.
    Rate is clamped at input; SPEC-003 applies additional per-profile
    safety clamps (≤1% body weight / week) at compute time.
    """

    goal_type: str = "maintain"  # maintain, lose_weight, gain_weight, muscle, etc.
    target_weight_kg: Optional[float] = Field(default=None, ge=20, le=400)
    rate_kg_per_week: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    started_at: Optional[str] = None
    paused_at: Optional[str] = None
    notes: str = ""


# --- Restriction resolution (SPEC-006) -----------------------------------


class ResolvedRestriction(BaseModel):
    """One user restriction string resolved to canonical tags."""

    raw: str
    allergen_tags: List[AllergenTag] = Field(default_factory=list)
    dietary_tags_forbid: List[DietaryTag] = Field(default_factory=list)
    dietary_tags_require: List[DietaryTag] = Field(default_factory=list)
    matched_canonical_ids: List[str] = Field(default_factory=list)
    confidence: float = 1.0
    source: str = "user"  # "user" | "shorthand" | "clinician"
    rule: str = "exact_alias"  # exact_alias|shorthand|category|negation|fuzzy
    soft_constraint: Optional[str] = None  # e.g. "low_carb", "sodium_very_high"
    note: str = ""


class AmbiguousRestriction(BaseModel):
    """A user input that mapped to multiple plausible resolutions.

    SPEC-006 §4.3: e.g. ``"nuts"`` could mean peanut, tree_nut, or both.
    The resolver never picks silently — it emits candidates and a
    human-readable question. Default-strict: the strictest candidate's
    tags are still exposed via ``RestrictionResolution.active_*`` so
    SPEC-007 can fail-closed before the user confirms.
    """

    raw: str
    candidates: List[ResolvedRestriction]  # len >= 2
    question: str


class RestrictionResolution(BaseModel):
    """SPEC-006 §4.1 — canonical tag resolution of a profile's raw lists."""

    resolved: List[ResolvedRestriction] = Field(default_factory=list)
    ambiguous: List[AmbiguousRestriction] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list)
    kb_version: str = ""
    resolved_at: Optional[str] = None

    def active_allergen_tags(self) -> Set[AllergenTag]:
        """Union of resolved allergen tags plus the strictest candidate
        from each unresolved ambiguity (default-strict, spec §6.2)."""
        out: Set[AllergenTag] = set()
        for r in self.resolved:
            out.update(r.allergen_tags)
        for amb in self.ambiguous:
            strictest: Set[AllergenTag] = set()
            for cand in amb.candidates:
                strictest.update(cand.allergen_tags)
            out.update(strictest)
        return out

    def active_dietary_forbid(self) -> Set[DietaryTag]:
        """Union of resolved dietary-forbid tags plus the strictest
        candidate from each unresolved ambiguity (default-strict)."""
        out: Set[DietaryTag] = set()
        for r in self.resolved:
            out.update(r.dietary_tags_forbid)
        for amb in self.ambiguous:
            strictest: Set[DietaryTag] = set()
            for cand in amb.candidates:
                strictest.update(cand.dietary_tags_forbid)
            out.update(strictest)
        return out


class ClientProfile(BaseModel):
    """Single source of truth for nutritionist and meal planning agents."""

    client_id: str = ""
    household: HouseholdInfo = Field(default_factory=HouseholdInfo)
    dietary_needs: List[str] = Field(
        default_factory=list
    )  # vegetarian, vegan, keto, low-sodium, diabetic-friendly, etc.
    allergies_and_intolerances: List[str] = Field(
        default_factory=list
    )  # nuts, shellfish, gluten, etc.
    lifestyle: LifestyleInfo = Field(default_factory=LifestyleInfo)
    preferences: PreferencesInfo = Field(default_factory=PreferencesInfo)
    goals: GoalsInfo = Field(default_factory=GoalsInfo)
    biometrics: BiometricInfo = Field(default_factory=BiometricInfo)
    clinical: ClinicalInfo = Field(default_factory=ClinicalInfo)
    # SPEC-006: canonical tag resolution of the raw restriction lists.
    # Populated by the resolver at intake time when the feature flag is on;
    # otherwise left as the empty default.
    restriction_resolution: RestrictionResolution = Field(default_factory=RestrictionResolution)
    # Monotonic write counter; bumped by the store on every save.
    profile_version: int = 1
    # Data-model version. Migrations that reshape ClientProfile bump
    # this; downstream consumers pin on it.
    schema_version: str = PROFILE_SCHEMA_VERSION
    updated_at: Optional[str] = None


# --- Nutrition plan (output of nutritionist agent) ---


class DailyTargets(BaseModel):
    """Daily nutrient targets."""

    calories_kcal: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    other_nutrients: Dict[str, float] = Field(default_factory=dict)


class PlanCohort(str, Enum):
    """Cohort tag set on a NutritionPlan by SPEC-004.

    Mirrors the ``nutrition_calc.Cohort`` string constants so the UI
    (SPEC-022) can branch on a single, stable enum exposed through the
    public API.
    """

    general_adult = "general_adult"
    general_adult_sex_unspecified = "general_adult_sex_unspecified"
    pregnancy_lactation = "pregnancy_lactation"
    ed_adjacent = "ed_adjacent"
    minor = "minor"
    clinician_guided = "clinician_guided"


class NutritionPlan(BaseModel):
    """Structured nutrition plan: targets and guidelines, no recipes.

    SPEC-004 additions (all additive, defaults preserve the legacy
    shape for any unmigrated consumer):

    - ``rationale``: structured audit trail from nutrition_calc.
      Stored as a dict so the JSONB column round-trips cleanly; the
      ``nutrition_calc.Rationale`` dataclass is not Pydantic-native.
    - ``calculator_version``: pinned by the SPEC-003 calculator.
      Downstream caches (SPEC-022) use this for cache invalidation.
    - ``cohort``: which SPEC-004 cohort produced the plan.
    - ``is_guidance_only``: True when the cohort is unsupported for
      numeric targets (minor / clinician_guided) or when the profile
      is incomplete (missing biometrics).
    - ``clinician_note``: user-facing note attached to guidance-only
      plans, e.g. "please work with your clinician".
    - ``intermediates``: BMR, TDEE for the "why these numbers?" panel.
    """

    daily_targets: DailyTargets = Field(default_factory=DailyTargets)
    balance_guidelines: List[str] = Field(
        default_factory=list
    )  # e.g. "more vegetables", "limit added sugar"
    foods_to_emphasize: List[str] = Field(default_factory=list)
    foods_to_avoid: List[str] = Field(default_factory=list)
    notes: str = ""
    generated_at: Optional[str] = None

    # SPEC-004 additive fields.
    rationale: Optional[Dict[str, Any]] = None
    calculator_version: Optional[str] = None
    cohort: PlanCohort = PlanCohort.general_adult
    is_guidance_only: bool = False
    clinician_note: Optional[str] = None
    intermediates: Dict[str, float] = Field(default_factory=dict)
    # Downstream consumers (ADR-003 rollup, SPEC-022 dashboard) read
    # metadata set by the calculator's clinical-override chain.
    metadata: Dict[str, Any] = Field(default_factory=dict)


# --- Meal recommendation (output of meal planning agent) ---


class MealRecommendation(BaseModel):
    """A single recipe/meal suggestion with rationale."""

    name: str = ""
    ingredients: List[str] = Field(default_factory=list)
    portions_servings: str = ""
    prep_time_minutes: Optional[int] = None
    cook_time_minutes: Optional[int] = None
    rationale: str = ""  # fits plan, time, preferences, past hits
    meal_type: str = ""  # lunch, dinner, breakfast, snack
    suggested_date: Optional[str] = None  # for calendar placement


class MealRecommendationWithId(MealRecommendation):
    """Meal recommendation plus storage id for feedback."""

    recommendation_id: str = ""


# --- Feedback ---


class FeedbackRecord(BaseModel):
    """User feedback on a recommended meal."""

    recommendation_id: str = ""
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    would_make_again: Optional[bool] = None
    notes: str = ""
    submitted_at: Optional[str] = None


# --- Meal history entry (recommendation + optional feedback) ---


class MealHistoryEntry(BaseModel):
    """One past recommendation with optional feedback."""

    recommendation_id: str = ""
    client_id: str = ""
    meal_snapshot: Dict[str, Any] = Field(default_factory=dict)  # stored recommendation payload
    recommended_at: Optional[str] = None
    feedback: Optional[FeedbackRecord] = None


# --- API request/response models ---


class ProfileUpdateRequest(BaseModel):
    """Body for PUT /profile/{client_id}.

    All fields optional and additive; unspecified fields preserve the
    current value. Nested objects merge shallowly (see SPEC-002 §4.4).
    """

    household: Optional[HouseholdInfo] = None
    dietary_needs: Optional[List[str]] = None
    allergies_and_intolerances: Optional[List[str]] = None
    lifestyle: Optional[LifestyleInfo] = None
    preferences: Optional[PreferencesInfo] = None
    goals: Optional[GoalsInfo] = None
    biometrics: Optional[BiometricInfo] = None
    clinical: Optional[ClinicalInfo] = None


# --- SPEC-006 request models --------------------------------------------


class ResolveAmbiguousRequest(BaseModel):
    """Body for POST /profile/{client_id}/restrictions/resolve-ambiguous.

    ``raw`` identifies the ambiguity (must match an entry in
    ``RestrictionResolution.ambiguous[*].raw``). ``chosen_candidate`` is
    the candidate the user picked; it is moved into ``resolved[]``.
    """

    raw: str
    chosen_candidate: ResolvedRestriction


# --- SPEC-002 additive request/response models ---------------------------


class BiometricPatchRequest(BaseModel):
    """Body for PATCH /profile/{client_id}/biometrics.

    Supports either metric inputs (``height_cm`` / ``weight_kg``) or
    imperial inputs (``height_ft`` + ``height_in`` / ``weight_lb``).
    Metric values win if both sides are provided. See ``units.py``.
    """

    sex: Optional[Sex] = None
    age_years: Optional[int] = Field(default=None, ge=2, le=120)
    height_cm: Optional[float] = Field(default=None, ge=50, le=260)
    height_ft: Optional[float] = Field(default=None, ge=0, le=9)
    height_in: Optional[float] = Field(default=None, ge=0, le=107)
    weight_kg: Optional[float] = Field(default=None, ge=20, le=400)
    weight_lb: Optional[float] = Field(default=None, ge=44, le=880)
    body_fat_pct: Optional[float] = Field(default=None, ge=3, le=75)
    activity_level: Optional[ActivityLevel] = None
    timezone: Optional[str] = None
    preferred_units: Optional[str] = None
    measured_at: Optional[str] = None

    @field_validator("preferred_units")
    @classmethod
    def _validate_units(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ("metric", "imperial"):
            raise ValueError("preferred_units must be 'metric' or 'imperial'")
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        # Validate at the request boundary so the orchestrator's
        # direct assignment (`bio.timezone = patch.timezone`) can't
        # persist an unknown zone that later breaks profile reads.
        if v is None:
            return v
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {v}") from exc
        except Exception:
            # Missing tzdata on host — accept (same tolerance as
            # BiometricInfo._validate_timezone).
            return v
        return v


class ClinicalPatchRequest(BaseModel):
    """Body for PATCH /profile/{client_id}/clinical.

    Whole-list semantics: ``conditions`` and ``medications`` replace the
    existing lists. This matches how users think about these edits
    (toggle chips on/off) and avoids subtle add/remove bugs.

    Clinician overrides are **not** editable through this path; use the
    admin-only ``PUT /clinical-overrides`` endpoint.
    """

    conditions: Optional[List[str]] = None
    medications: Optional[List[str]] = None
    reproductive_state: Optional[ReproductiveState] = None
    ed_history_flag: Optional[bool] = None


class ClinicianOverrideRequest(BaseModel):
    """Body for PUT /profile/{client_id}/clinical-overrides.

    Admin-only. Every write produces an audit row. Overrides replace
    the entire dict; partial edits go through the admin tool, not the
    API.
    """

    overrides: Dict[str, float] = Field(default_factory=dict)
    reason: Optional[str] = None
    author: str = "admin"


class BiometricHistoryEntry(BaseModel):
    """One row from nutrition_biometric_log, trimmed for the API."""

    field: str
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    unit: Optional[str] = None
    source: str = "manual"
    recorded_at: str
    recorded_by: Optional[str] = None


class BiometricHistoryResponse(BaseModel):
    client_id: str
    field: Optional[str] = None
    entries: List[BiometricHistoryEntry] = Field(default_factory=list)


class CompletenessResponse(BaseModel):
    """Shape for GET /profile/{client_id}/completeness.

    Drives the UI gating: a profile with no ``weight_kg`` for instance
    cannot yet receive calculator-driven plans and the UI shows a
    banner. See SPEC-002 §4.5 / §4.6.
    """

    client_id: str
    has_biometrics: bool = False
    has_activity: bool = False
    has_clinical_confirmed: bool = False
    is_minor: bool = False
    ed_history_flag: bool = False
    blockers: List[str] = Field(default_factory=list)


class NutritionPlanRequest(BaseModel):
    """Body for POST /plan/nutrition."""

    client_id: str
    date_range_start: Optional[str] = None
    date_range_end: Optional[str] = None


class NutritionPlanResponse(BaseModel):
    """Response for POST /plan/nutrition."""

    client_id: str
    plan: NutritionPlan


class MealPlanRequest(BaseModel):
    """Body for POST /plan/meals."""

    client_id: str
    period_days: int = 7
    meal_types: List[str] = Field(default_factory=lambda: ["lunch", "dinner"])


class MealPlanResponse(BaseModel):
    """Response for POST /plan/meals."""

    client_id: str
    suggestions: List[MealRecommendationWithId] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    """Body for POST /feedback."""

    client_id: str
    recommendation_id: str
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    would_make_again: Optional[bool] = None
    notes: Optional[str] = None


class FeedbackResponse(BaseModel):
    """Response for POST /feedback."""

    recommendation_id: str
    recorded: bool = True


class MealHistoryResponse(BaseModel):
    """Response for GET /history/meals."""

    client_id: str
    entries: List[MealHistoryEntry] = Field(default_factory=list)


# --- Chat models ---


class ChatMessage(BaseModel):
    """One message in the conversation history."""

    role: str = "user"  # "user" or "assistant"
    content: str = ""


class ChatRequest(BaseModel):
    """Body for POST /chat."""

    client_id: str
    message: str
    conversation_history: List[ChatMessage] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """Response for POST /chat.  Always contains the agent message and current phase.
    Optionally contains structured results when the agent triggers an action."""

    message: str = ""
    phase: str = "intake"
    action: str = "none"
    profile: Optional[ClientProfile] = None
    nutrition_plan: Optional[NutritionPlan] = None
    meal_suggestions: List[MealRecommendationWithId] = Field(default_factory=list)
    feedback_recorded: bool = False
