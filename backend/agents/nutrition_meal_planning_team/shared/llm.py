"""LLM client for Nutrition & Meal Planning team. Reuses Personal Assistant LLM (Ollama + JSON extraction)."""

from __future__ import annotations

from personal_assistant_team.shared.llm import (
    JSONExtractionFailure,
    LLMClient,
    get_llm_client as _pa_get_llm_client,
)


def get_llm_client(agent_key: str = "nutrition_meal_planning") -> LLMClient:
    """Return LLM client for this team. Uses PA team's client with optional agent-specific model."""
    return _pa_get_llm_client(agent_key)
