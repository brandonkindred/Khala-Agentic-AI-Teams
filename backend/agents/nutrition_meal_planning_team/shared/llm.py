"""LLM client for Nutrition & Meal Planning team. Uses central llm_service."""

from __future__ import annotations

from llm_service import LLMJsonParseError, get_strands_model
from strands import Agent
from strands.models.ollama import OllamaModel

# Backward compat: JSONExtractionFailure used by agents that catch parse failures
JSONExtractionFailure = LLMJsonParseError


def get_llm_model(agent_key: str = "nutrition_meal_planning") -> OllamaModel:
    """Return Strands model for this team. Delegates to central llm_service."""
    return get_strands_model(agent_key)
