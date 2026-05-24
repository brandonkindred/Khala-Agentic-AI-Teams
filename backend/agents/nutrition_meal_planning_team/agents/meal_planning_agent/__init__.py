"""Meal planning agent: recipe/meal suggestions from profile, nutrition plan, and meal history."""

from .agent import MealPlanningAgent, _build_user_prompt, _summarize_history
from .prompt_constraints import render_constraints_block
from .regeneration_prompt import REGENERATION_SYSTEM_PROMPT, build_regeneration_prompt

__all__ = [
    "MealPlanningAgent",
    "REGENERATION_SYSTEM_PROMPT",
    "_build_user_prompt",
    "_summarize_history",
    "build_regeneration_prompt",
    "render_constraints_block",
]
