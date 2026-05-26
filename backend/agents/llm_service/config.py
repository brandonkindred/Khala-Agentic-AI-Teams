"""
Single source of configuration for the LLM service.

Environment variables use LLM_* prefix. Known model context and
per-agent default models live here.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable names (LLM_*)
# ---------------------------------------------------------------------------

ENV_LLM_PROVIDER = "LLM_PROVIDER"
ENV_LLM_MODEL = "LLM_MODEL"
ENV_LLM_BASE_URL = "LLM_BASE_URL"
ENV_LLM_TIMEOUT = "LLM_TIMEOUT"
ENV_LLM_CONTEXT_SIZE = "LLM_CONTEXT_SIZE"
ENV_LLM_MAX_TOKENS = "LLM_MAX_TOKENS"
ENV_LLM_MAX_RETRIES = "LLM_MAX_RETRIES"
ENV_LLM_BACKOFF_BASE = "LLM_BACKOFF_BASE"
ENV_LLM_BACKOFF_MAX = "LLM_BACKOFF_MAX"
ENV_LLM_MAX_CONCURRENCY = "LLM_MAX_CONCURRENCY"
ENV_LLM_ENABLE_THINKING = "LLM_ENABLE_THINKING"
ENV_LLM_OLLAMA_API_KEY = "LLM_OLLAMA_API_KEY"

# Default cap for max_tokens (many APIs limit output to 32K even when context is 256K)
DEFAULT_MAX_OUTPUT_TOKENS = 32768

# ---------------------------------------------------------------------------
# Known model context (tokens) – used when /api/show is unavailable or not called
# ---------------------------------------------------------------------------

KNOWN_MODEL_CONTEXT: dict[str, int] = {
    "qwen3.5:397b": 262144,
    "qwen3.5:397b-cloud": 262144,
    "qwen3.5:cloud": 262144,
    "qwen3-coder:480b-cloud": 262144,
    "qwen3-coder:480b": 262144,
    # deepseek-v4-pro:cloud is intentionally omitted — context size is
    # resolved at runtime via the Ollama /api/show call on first use.
}

# ---------------------------------------------------------------------------
# Per-agent default model when LLM_MODEL_<agent_key> and LLM_MODEL are unset
# ---------------------------------------------------------------------------

AGENT_DEFAULT_MODELS: dict[str, str] = {
    "backend": "deepseek-v4-pro:cloud",
    "frontend": "deepseek-v4-pro:cloud",
    "code_review": "deepseek-v4-pro:cloud",
    "repair": "deepseek-v4-pro:cloud",
    "devops": "deepseek-v4-pro:cloud",
    "dbc_comments": "deepseek-v4-pro:cloud",
    "tech_lead": "deepseek-v4-pro:cloud",
    "architecture": "deepseek-v4-pro:cloud",
    "spec_intake": "deepseek-v4-pro:cloud",
    "spec_clarification": "deepseek-v4-pro:cloud",
    "product_analysis": "deepseek-v4-pro:cloud",
    "project_planning": "deepseek-v4-pro:cloud",
    "integration": "deepseek-v4-pro:cloud",
    "api_contract": "deepseek-v4-pro:cloud",
    "data_architecture": "deepseek-v4-pro:cloud",
    "ui_ux": "deepseek-v4-pro:cloud",
    "frontend_architecture": "deepseek-v4-pro:cloud",
    "infrastructure": "deepseek-v4-pro:cloud",
    "devops_planning": "deepseek-v4-pro:cloud",
    "qa_test_strategy": "deepseek-v4-pro:cloud",
    "security_planning": "deepseek-v4-pro:cloud",
    "observability": "deepseek-v4-pro:cloud",
    "acceptance_verifier": "deepseek-v4-pro:cloud",
    "documentation": "deepseek-v4-pro:cloud",
    "qa": "deepseek-v4-pro:cloud",
    "security": "deepseek-v4-pro:cloud",
    "accessibility": "deepseek-v4-pro:cloud",
    # Other teams
    "soc2": "llama3.1",
    "blog": "deepseek-v4-pro:cloud",
    "personal_assistant": "llama3.2",
    "nutrition_meal_planning": "llama3.2",
    "accessibility_audit": "llama3.1",
    "strategy_ideation": "deepseek-v4-pro:cloud",
    "signal_intelligence": "deepseek-v4-pro:cloud",
    "deepthought": "deepseek-v4-pro:cloud",
}

DEFAULT_FALLBACK_MODEL = "deepseek-v4-pro:cloud"

# ---------------------------------------------------------------------------
# Resolvers (env + agent defaults)
# ---------------------------------------------------------------------------


def resolve_provider() -> str:
    """Return effective LLM provider: 'dummy' or 'ollama' (default)."""
    return (os.environ.get(ENV_LLM_PROVIDER) or "ollama").lower().strip()


def resolve_model(agent_key: Optional[str] = None) -> str:
    """
    Resolve model name: LLM_MODEL_<agent_key>, then LLM_MODEL, then AGENT_DEFAULT_MODELS[agent_key], then fallback.
    """
    if agent_key:
        per_agent = os.environ.get(f"LLM_MODEL_{agent_key}")
        if per_agent:
            return per_agent.strip()
    global_model = (os.environ.get(ENV_LLM_MODEL) or "").strip()
    if global_model:
        return global_model
    if agent_key and agent_key in AGENT_DEFAULT_MODELS:
        return AGENT_DEFAULT_MODELS[agent_key]
    return DEFAULT_FALLBACK_MODEL


def resolve_base_url() -> str:
    """Return Ollama base URL (default https://ollama.com for Ollama Cloud)."""
    return (os.environ.get(ENV_LLM_BASE_URL) or "https://ollama.com").strip().rstrip("/")


def resolve_timeout(agent_key: Optional[str] = None) -> float:
    """Return timeout in seconds (default 900 — 15 min).

    All LLM calls use streaming, so the timeout covers the full streamed response.
    Override with LLM_TIMEOUT if needed.
    """
    raw = os.environ.get(ENV_LLM_TIMEOUT) or "900"
    try:
        return float(raw)
    except ValueError:
        return 900.0


def resolve_context_size_for_model(model: str) -> Optional[int]:
    """
    Resolve context size (tokens) for a model: env LLM_CONTEXT_SIZE (global override),
    then KNOWN_MODEL_CONTEXT[model], else None (caller may use /api/show or default).
    """
    raw = os.environ.get(ENV_LLM_CONTEXT_SIZE)
    if raw:
        try:
            return max(2048, int(raw))
        except ValueError:
            pass
    return KNOWN_MODEL_CONTEXT.get(model)


def get_llm_config_summary() -> str:
    """Return a short summary of effective provider and model for logging."""
    provider = resolve_provider()
    if provider == "ollama":
        model = resolve_model(None)
        base_url = resolve_base_url()
        return f"provider={provider}, model={model}, base_url={base_url}"
    return f"provider={provider}"
