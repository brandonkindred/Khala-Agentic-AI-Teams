"""LLM client for Nutrition & Meal Planning team. Uses central llm_service."""

from __future__ import annotations

from llm_service import LLMClient, LLMJsonParseError, get_client

# Backward compat: JSONExtractionFailure used by agents that catch parse failures
JSONExtractionFailure = LLMJsonParseError


def get_llm_client(agent_key: str = "nutrition_meal_planning") -> LLMClient:
    """Return LLM client for this team. Delegates to central llm_service."""
    return get_client(agent_key)
