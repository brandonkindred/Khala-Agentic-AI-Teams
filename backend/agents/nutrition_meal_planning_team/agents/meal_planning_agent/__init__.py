"""Meal planning agent: recipe/meal suggestions from profile, nutrition plan, and meal history."""

from .agent import MealPlanningAgent, _build_user_prompt, _summarize_history
from .prompt_constraints import render_constraints_block

__all__ = [
    "MealPlanningAgent",
    "_build_user_prompt",
    "_summarize_history",
    "render_constraints_block",
]
