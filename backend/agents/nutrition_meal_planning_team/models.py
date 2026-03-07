"""Pydantic models for Nutrition & Meal Planning team."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --- Client profile (what a dietician would need) ---


class HouseholdInfo(BaseModel):
    """Who is being fed."""

    number_of_people: int = 1
    description: str = ""  # e.g. "solo", "couple", "family of 4"
    ages_if_relevant: List[str] = Field(default_factory=list)  # e.g. ["adult", "child 8"]


class LifestyleInfo(BaseModel):
    """Lifestyle and busyness constraints."""

    max_cooking_time_minutes: Optional[int] = None  # e.g. 15, 30
    lunch_context: str = "remote"  # "office" (portable, minimal prep) or "remote" (can cook/reheat)
    equipment_constraints: List[str] = Field(default_factory=list)  # e.g. "no oven", "limited equipment"
    other_constraints: str = ""


class PreferencesInfo(BaseModel):
    """Food and drink preferences."""

    cuisines_liked: List[str] = Field(default_factory=list)
    cuisines_disliked: List[str] = Field(default_factory=list)
    ingredients_disliked: List[str] = Field(default_factory=list)
    preferences_free_text: str = ""  # "prefer X over Y", etc.


class GoalsInfo(BaseModel):
    """Nutrition goals (optional)."""

    goal_type: str = "maintain"  # maintain, lose_weight, gain_weight, muscle, etc.
    notes: str = ""


class ClientProfile(BaseModel):
    """Single source of truth for nutritionist and meal planning agents."""

    client_id: str = ""
    household: HouseholdInfo = Field(default_factory=HouseholdInfo)
    dietary_needs: List[str] = Field(default_factory=list)  # vegetarian, vegan, keto, low-sodium, diabetic-friendly, etc.
    allergies_and_intolerances: List[str] = Field(default_factory=list)  # nuts, shellfish, gluten, etc.
    lifestyle: LifestyleInfo = Field(default_factory=LifestyleInfo)
    preferences: PreferencesInfo = Field(default_factory=PreferencesInfo)
    goals: GoalsInfo = Field(default_factory=GoalsInfo)
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


class NutritionPlan(BaseModel):
    """Structured nutrition plan: targets and guidelines, no recipes."""

    daily_targets: DailyTargets = Field(default_factory=DailyTargets)
    balance_guidelines: List[str] = Field(default_factory=list)  # e.g. "more vegetables", "limit added sugar"
    foods_to_emphasize: List[str] = Field(default_factory=list)
    foods_to_avoid: List[str] = Field(default_factory=list)
    notes: str = ""
    generated_at: Optional[str] = None


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
    rating: Optional[int] = None  # e.g. 1-5
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
    """Body for PUT /profile/{client_id}."""

    household: Optional[HouseholdInfo] = None
    dietary_needs: Optional[List[str]] = None
    allergies_and_intolerances: Optional[List[str]] = None
    lifestyle: Optional[LifestyleInfo] = None
    preferences: Optional[PreferencesInfo] = None
    goals: Optional[GoalsInfo] = None


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
    rating: Optional[int] = None  # 1-5
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
