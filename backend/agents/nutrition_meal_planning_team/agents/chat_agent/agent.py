"""Conversational nutritionist chat agent.

Wraps the existing intake, nutritionist, and meal-planning agents behind a
single chat interface.  The LLM acts as a friendly nutritionist who gathers
profile information through probing questions, then triggers the underlying
agents when enough data has been collected.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from strands import Agent

from ...models import (
    ClientProfile,
    MealHistoryEntry,
    MealRecommendationWithId,
    NutritionPlan,
)
from ..intake_profile_agent import IntakeProfileAgent
from ..meal_planning_agent import MealPlanningAgent
from ..nutritionist_agent import NutritionistAgent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PHASES = ["intake", "nutrition", "meals", "feedback"]


def _profile_completeness(profile: Optional[ClientProfile]) -> Dict[str, bool]:
    """Return which profile sections have meaningful data."""
    if profile is None:
        return {
            "household": False,
            "dietary_needs": False,
            "lifestyle": False,
            "preferences": False,
            "goals": False,
        }
    hh = profile.household
    has_household = bool(hh.number_of_people > 0 and (hh.description or len(hh.members) > 0))
    return {
        "household": has_household,
        "dietary_needs": len(profile.dietary_needs) > 0
        or len(profile.allergies_and_intolerances) > 0,
        "lifestyle": profile.lifestyle.max_cooking_time_minutes is not None
        or bool(profile.lifestyle.other_constraints),
        "preferences": bool(
            profile.preferences.cuisines_liked
            or profile.preferences.cuisines_disliked
            or profile.preferences.ingredients_disliked
            or profile.preferences.preferences_free_text
        ),
        "goals": profile.goals.goal_type != "maintain" or bool(profile.goals.notes),
    }


def _current_phase(
    profile: Optional[ClientProfile], has_nutrition_plan: bool, has_meals: bool
) -> str:
    """Determine which phase the user is in based on data state."""
    if profile is None:
        return "intake"
    comp = _profile_completeness(profile)
    if not comp["household"]:
        return "intake"
    if not has_nutrition_plan:
        # Profile exists; if most sections filled we can move on
        filled = sum(1 for v in comp.values() if v)
        if filled < 2:
            return "intake"
        return "nutrition"
    if not has_meals:
        return "meals"
    return "feedback"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a warm, knowledgeable nutritionist assistant.  You help families and
individuals plan healthy, practical meals through friendly conversation.

## Your conversation flow follows these phases:

### Phase 1 — intake (Getting to know the household)
Gather the following through natural conversation.  Ask ONE or TWO questions
at a time, not a long list.  Be warm and conversational.
- Who you're cooking for (household members, ages, roles)
- Any dietary needs (vegetarian, vegan, keto, low-sodium, diabetic-friendly…)
- Allergies or intolerances
- Lifestyle: typical max cooking time, whether lunches need to be portable
  (office) or can be cooked at home, kitchen equipment limits
- Food preferences: cuisines loved/disliked, ingredients to avoid, free-text
  likes/dislikes
- Goals: maintain weight, lose weight, gain weight, build muscle, or other

When the user has provided enough detail for ALL of the above areas (even
briefly), set action to "save_profile" so their info is saved.  You do NOT
need every field — sensible defaults are fine.  But you MUST have at least
household info and one other area before saving.

### Phase 2 — nutrition (Nutrition snapshot)
After the profile is saved, briefly explain you'll create a nutrition
snapshot.  Set action to "generate_nutrition_plan".  Then present the results
conversationally and ask if they'd like to adjust anything or move to meal
planning.

### Phase 3 — meals (Weekly meal plan)
When the user is ready for meals, ask how many days and which meal types
(breakfast, lunch, dinner, snack).  Set action to "generate_meals" with
the parameters.

### Phase 4 — feedback
After meals are generated, ask what they think.  When they rate a meal or
say they liked/disliked it, set action to "submit_feedback" with the details.
They can always ask for a new meal plan, adjust their profile, etc.

## Response format

You MUST respond with valid JSON (no markdown fences):
{
  "message": "<your conversational response to the user>",
  "phase": "<intake|nutrition|meals|feedback>",
  "action": "<none|save_profile|generate_nutrition_plan|generate_meals|submit_feedback>",
  "extracted_profile": {
    "household": {"number_of_people": ..., "description": "...", "members": [...]},
    "dietary_needs": [...],
    "allergies_and_intolerances": [...],
    "lifestyle": {"max_cooking_time_minutes": ..., "lunch_context": "...", "equipment_constraints": [...]},
    "preferences": {"cuisines_liked": [...], "cuisines_disliked": [...], "ingredients_disliked": [...], "preferences_free_text": "..."},
    "goals": {"goal_type": "...", "notes": "..."}
  },
  "meal_plan_params": {"period_days": 7, "meal_types": ["lunch", "dinner"]},
  "feedback_data": {"meal_name": "...", "rating": null, "would_make_again": null, "notes": ""}
}

Rules for the JSON:
- "extracted_profile": include ALL fields the user has mentioned across the
  ENTIRE conversation (not just this turn).  Omit fields you truly have no
  info for (don't guess allergies).  This gets merged into the stored profile
  — omitted fields are preserved, included fields overwrite.
- "meal_plan_params": only needed when action is "generate_meals".
- "feedback_data": only needed when action is "submit_feedback".
- "action": set to "none" if you're just asking questions or chatting.
- Always include "message" and "phase".
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class NutritionChatAgent:
    """Conversational nutritionist that delegates to specialist agents."""

    def __init__(
        self,
        model: Any,
        intake_agent: IntakeProfileAgent,
        nutritionist_agent: NutritionistAgent,
        meal_planning_agent: MealPlanningAgent,
    ) -> None:
        self._agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)
        self.intake_agent = intake_agent
        self.nutritionist_agent = nutritionist_agent
        self.meal_planning_agent = meal_planning_agent

    def run(
        self,
        client_id: str,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        profile: Optional[ClientProfile],
        nutrition_plan: Optional[NutritionPlan],
        meal_suggestions: Optional[List[MealRecommendationWithId]],
        meal_history: Optional[List[MealHistoryEntry]] = None,
    ) -> Dict[str, Any]:
        """
        Process one chat turn.

        Returns dict with keys:
          message (str), phase (str), action (str),
          extracted_profile (dict|None),
          meal_plan_params (dict|None),
          feedback_data (dict|None)
        """
        has_nutrition = nutrition_plan is not None and bool(nutrition_plan.balance_guidelines)
        has_meals = bool(meal_suggestions)
        phase = _current_phase(profile, has_nutrition, has_meals)

        # Build the user prompt with context
        context_parts: List[str] = []
        context_parts.append(f"Client ID: {client_id}")
        context_parts.append(f"Current phase: {phase}")

        comp = _profile_completeness(profile)
        context_parts.append(f"Profile completeness: {json.dumps(comp)}")

        if profile:
            context_parts.append(f"Current profile:\n{profile.model_dump_json(indent=2)}")

        if nutrition_plan and has_nutrition:
            context_parts.append(
                f"Current nutrition plan:\n{nutrition_plan.model_dump_json(indent=2)}"
            )

        if meal_suggestions:
            meals_summary = [
                {
                    "name": m.name,
                    "meal_type": m.meal_type,
                    "recommendation_id": m.recommendation_id,
                    "suggested_date": m.suggested_date,
                }
                for m in meal_suggestions
            ]
            context_parts.append(
                f"Current meal suggestions:\n{json.dumps(meals_summary, indent=2)}"
            )

        # Build conversation for the LLM
        history_text = ""
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            history_text += f"\n[{role}]: {content}"

        prompt = (
            "--- CONTEXT ---\n"
            + "\n".join(context_parts)
            + "\n\n--- CONVERSATION HISTORY ---"
            + history_text
            + f"\n\n[user]: {user_message}"
            + "\n\n--- YOUR RESPONSE (valid JSON only, no markdown fences) ---"
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
        except Exception as e:
            logger.warning("Chat agent LLM call failed: %s", e)
            return {
                "message": "I'm sorry, I had trouble processing that. Could you try rephrasing?",
                "phase": phase,
                "action": "none",
                "extracted_profile": None,
                "meal_plan_params": None,
                "feedback_data": None,
            }

        # Normalize response
        result_dict: Dict[str, Any] = {
            "message": data.get("message", ""),
            "phase": data.get("phase", phase),
            "action": data.get("action", "none"),
            "extracted_profile": data.get("extracted_profile"),
            "meal_plan_params": data.get("meal_plan_params"),
            "feedback_data": data.get("feedback_data"),
        }
        return result_dict
