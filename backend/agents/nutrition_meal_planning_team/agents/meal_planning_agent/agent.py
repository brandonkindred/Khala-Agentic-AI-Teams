"""Meal planning agent: suggests recipes/meals that fit plan, time, preferences, and past feedback."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional, Sequence

from strands import Agent

from ...guardrail.violations import Violation
from ...models import (
    ClientProfile,
    MealHistoryEntry,
    MealRecommendation,
    NutritionPlan,
)
from .prompt_constraints import render_constraints_block
from .regeneration_prompt import REGENERATION_SYSTEM_PROMPT, build_regeneration_prompt

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

Respect max_cooking_time_minutes and lunch_context (office = portable/minimal prep). Prefer meals similar to past "hits" (high rating / would make again) and avoid ones like past "misses". If a DIETARY CONSTRAINTS block is provided below, you MUST obey every FORBIDDEN entry exactly. Never include a forbidden allergen or dietary category. Output only valid JSON."""


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


def _build_user_prompt(
    profile: ClientProfile,
    nutrition_plan: NutritionPlan,
    history_summary: str,
    period_days: int,
    meal_types: List[str],
) -> str:
    """Assemble the user prompt with optional constraints block."""
    parts = ["Client profile:\n" + profile.model_dump_json(indent=2)]

    try:
        constraints = render_constraints_block(profile.restriction_resolution, profile.clinical)
        if constraints:
            parts.append(constraints)
    except Exception as e:
        logger.error("Prompt constraint rendering failed, continuing without constraints: %s", e)

    parts.append(
        "Nutrition plan (targets and guidelines):\n" + nutrition_plan.model_dump_json(indent=2)
    )
    parts.append("Past meal feedback summary:\n" + history_summary)
    parts.append(
        "Request: suggest meals for the next "
        + str(period_days)
        + " days, meal types: "
        + ", ".join(meal_types)
        + '. Output JSON: {"suggestions": [ ... ]} with each item having name, ingredients, portions_servings,'
        " prep_time_minutes, cook_time_minutes, rationale, meal_type, suggested_date."
        "\n\nRespond with valid JSON only, no markdown fences."
    )

    return "\n\n".join(parts)


class MealPlanningAgent:
    """Suggests meals from profile, nutrition plan, and meal history. Caller records recommendations and passes history."""

    def __init__(self, model: Any, *, agent_key: str = "nutrition_meal_planning") -> None:
        self._agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)
        self._agent_key = agent_key
        self._client: Any = None

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

        prompt = _build_user_prompt(
            profile, nutrition_plan, history_summary, period_days, meal_types
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
        except Exception as e:
            logger.warning("Meal planning LLM call failed: %s", e)
            return []

        suggestions = data.get("suggestions") or []
        result_list: List[MealRecommendation] = []
        for s in suggestions:
            if isinstance(s, dict):
                result_list.append(MealRecommendation.model_validate(s))
        return result_list

    def _get_client(self) -> Any:
        if self._client is None:
            from llm_service import get_client

            self._client = get_client(self._agent_key)
        return self._client

    def regenerate_single(
        self,
        profile: ClientProfile,
        original: MealRecommendation,
        violations: Sequence[Violation],
    ) -> Optional[MealRecommendation]:
        """Generate a single replacement meal after a guardrail rejection.

        Preconditions:
            ``violations`` is non-empty — at least one hard-reject violation
            triggered the regeneration request.
            ``profile`` carries a valid ``restriction_resolution`` and ``clinical``.

        Postconditions:
            Returns a validated ``MealRecommendation`` on success, or ``None``
            when the LLM fails to produce a valid response after built-in
            self-correction retry.
        """
        from llm_service import LLMError, LLMPermanentError
        from llm_service.structured import complete_validated

        prompt = build_regeneration_prompt(profile, original, violations)
        try:
            client = self._get_client()
            return complete_validated(
                client,
                prompt,
                schema=MealRecommendation,
                system_prompt=REGENERATION_SYSTEM_PROMPT,
            )
        except LLMPermanentError as e:
            logger.warning("regenerate_single failed (permanent): %s", e)
            return None
        except LLMError as e:
            logger.warning("regenerate_single failed (transient): %s", e)
            return None
        except Exception as e:
            logger.exception("regenerate_single unexpected error: %s", e)
            return None
