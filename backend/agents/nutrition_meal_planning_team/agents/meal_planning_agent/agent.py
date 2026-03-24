"""Meal planning agent: suggests recipes/meals that fit plan, time, preferences, and past feedback."""

from __future__ import annotations

import logging
from typing import List, Optional

from llm_service import LLMClient, LLMError, LLMJsonParseError

from ...models import (
    ClientProfile,
    MealHistoryEntry,
    MealRecommendation,
    NutritionPlan,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a meal planning expert. Given a client profile, their nutrition plan, and past meal history (with feedback on what they liked or disliked), suggest concrete recipes/meals as JSON.

Output a JSON object with key "suggestions": array of objects, each with:
- name: string (recipe/meal name)
- ingredients: array of strings
- portions_servings: string
- prep_time_minutes: number or null
- cook_time_minutes: number or null
- rationale: string (why this fits: plan, time, preferences, or similar to past hits)
- meal_type: string (e.g. lunch, dinner, breakfast)
- suggested_date: string or null (e.g. for calendar)

Respect max_cooking_time_minutes and lunch_context (office = portable/minimal prep). Prefer meals similar to past "hits" (high rating / would make again) and avoid ones like past "misses". Output only valid JSON."""


def _summarize_history(entries: List[MealHistoryEntry]) -> str:
    """Build past hits / past misses summary for the prompt."""
    hits = []
    misses = []
    for e in entries:
        snap = e.meal_snapshot or {}
        name = snap.get("name") or snap.get("title") or "unknown"
        if e.feedback:
            if e.feedback.rating is not None and e.feedback.rating >= 4:
                hits.append(name)
            elif e.feedback.would_make_again is False or (
                e.feedback.rating is not None and e.feedback.rating <= 2
            ):
                misses.append(name)
    lines = []
    if hits:
        lines.append("Past hits (they liked these): " + ", ".join(hits[:15]))
    if misses:
        lines.append("Past misses (avoid similar): " + ", ".join(misses[:15]))
    return "\n".join(lines) if lines else "No past feedback yet."


class MealPlanningAgent:
    """Suggests meals from profile, nutrition plan, and meal history. Caller records recommendations and passes history."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(
        self,
        profile: ClientProfile,
        nutrition_plan: NutritionPlan,
        meal_history: List[MealHistoryEntry],
        period_days: int = 7,
        meal_types: Optional[List[str]] = None,
    ) -> List[MealRecommendation]:
        """Generate meal suggestions. Past hits/misses are summarized in the prompt."""
        meal_types = meal_types or ["lunch", "dinner"]
        history_summary = _summarize_history(meal_history)

        prompt = (
            "Client profile:\n"
            + profile.model_dump_json(indent=2)
            + "\n\nNutrition plan (targets and guidelines):\n"
            + nutrition_plan.model_dump_json(indent=2)
            + "\n\nPast meal feedback summary:\n"
            + history_summary
            + "\n\nRequest: suggest meals for the next "
            + str(period_days)
            + " days, meal types: "
            + ", ".join(meal_types)
            + '. Output JSON: {"suggestions": [ ... ]} with each item having name, ingredients, portions_servings, prep_time_minutes, cook_time_minutes, rationale, meal_type, suggested_date.'
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.4,
                system_prompt=SYSTEM_PROMPT,
                expected_keys=["suggestions"],
            )
        except (LLMJsonParseError, LLMError) as e:
            logger.warning("Meal planning LLM call failed: %s", e)
            return []

        suggestions = data.get("suggestions") or []
        result: List[MealRecommendation] = []
        for s in suggestions:
            if isinstance(s, dict):
                result.append(MealRecommendation.model_validate(s))
        return result
