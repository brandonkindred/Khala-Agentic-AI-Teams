"""
Single source of configuration for the LLM service.

Environment variables use LLM_* prefix with backward compatibility for SW_LLM_*
(and optionally BLOG_LLM_*, SOC2_LLM_* for migration). Known model context and
per-agent default models live here.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable names (prefer LLM_*, fallback to SW_LLM_*)
# ---------------------------------------------------------------------------

def _env(key_llm: str, key_sw: str, default: str) -> str:
    """Return env value: LLM_* if set, else SW_LLM_*, else default."""
    v = os.environ.get(key_llm) or os.environ.get(key_sw)
    return (v or default).strip()

# Primary (LLM_*) and backward-compat (SW_LLM_*)
ENV_LLM_PROVIDER = "LLM_PROVIDER"
ENV_LLM_PROVIDER_SW = "SW_LLM_PROVIDER"
ENV_LLM_MODEL = "LLM_MODEL"
ENV_LLM_MODEL_SW = "SW_LLM_MODEL"
ENV_LLM_BASE_URL = "LLM_BASE_URL"
ENV_LLM_BASE_URL_SW = "SW_LLM_BASE_URL"
ENV_LLM_TIMEOUT = "LLM_TIMEOUT"
ENV_LLM_TIMEOUT_SW = "SW_LLM_TIMEOUT"
ENV_LLM_CONTEXT_SIZE = "LLM_CONTEXT_SIZE"
ENV_LLM_CONTEXT_SIZE_SW = "SW_LLM_CONTEXT_SIZE"
ENV_LLM_MAX_TOKENS = "LLM_MAX_TOKENS"
ENV_LLM_MAX_TOKENS_SW = "SW_LLM_MAX_TOKENS"
ENV_LLM_MAX_RETRIES = "LLM_MAX_RETRIES"
ENV_LLM_MAX_RETRIES_SW = "SW_LLM_MAX_RETRIES"
ENV_LLM_BACKOFF_BASE = "LLM_BACKOFF_BASE"
ENV_LLM_BACKOFF_BASE_SW = "SW_LLM_BACKOFF_BASE"
ENV_LLM_BACKOFF_MAX = "LLM_BACKOFF_MAX"
ENV_LLM_BACKOFF_MAX_SW = "SW_LLM_BACKOFF_MAX_SECONDS"
ENV_LLM_MAX_CONCURRENCY = "LLM_MAX_CONCURRENCY"
ENV_LLM_MAX_CONCURRENCY_SW = "SW_LLM_MAX_CONCURRENCY"
ENV_LLM_ENABLE_THINKING = "LLM_ENABLE_THINKING"
ENV_LLM_ENABLE_THINKING_SW = "SW_LLM_ENABLE_THINKING"
ENV_LLM_OLLAMA_API_KEY = "LLM_OLLAMA_API_KEY"
ENV_LLM_OLLAMA_API_KEY_SW = "SW_LLM_OLLAMA_API_KEY"

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
}

# ---------------------------------------------------------------------------
# Per-agent default model when LLM_MODEL_<agent_key> and LLM_MODEL are unset
# ---------------------------------------------------------------------------

AGENT_DEFAULT_MODELS: dict[str, str] = {
    "backend": "qwen3.5:397b-cloud",
    "frontend": "qwen3.5:397b-cloud",
    "code_review": "qwen3.5:397b-cloud",
    "repair": "qwen3.5:397b-cloud",
    "devops": "qwen3.5:397b-cloud",
    "dbc_comments": "qwen3.5:397b-cloud",
    "tech_lead": "qwen3.5:397b-cloud",
    "architecture": "qwen3.5:397b-cloud",
    "spec_intake": "qwen3.5:397b-cloud",
    "spec_clarification": "qwen3.5:397b-cloud",
    "product_analysis": "qwen3.5:397b-cloud",
    "project_planning": "qwen3.5:397b-cloud",
    "integration": "qwen3.5:397b-cloud",
    "api_contract": "qwen3.5:397b-cloud",
    "data_architecture": "qwen3.5:397b-cloud",
    "ui_ux": "qwen3.5:397b-cloud",
    "frontend_architecture": "qwen3.5:397b-cloud",
    "infrastructure": "qwen3.5:397b-cloud",
    "devops_planning": "qwen3.5:397b-cloud",
    "qa_test_strategy": "qwen3.5:397b-cloud",
    "security_planning": "qwen3.5:397b-cloud",
    "observability": "qwen3.5:397b-cloud",
    "acceptance_verifier": "qwen3.5:397b-cloud",
    "documentation": "qwen3.5:397b-cloud",
    "qa": "qwen3.5:397b-cloud",
    "security": "qwen3.5:397b-cloud",
    "accessibility": "qwen3.5:397b-cloud",
    # Other teams
    "soc2": "llama3.1",
    "blog": "qwen3.5:397b-cloud",
    "personal_assistant": "llama3.2",
    "nutrition_meal_planning": "llama3.2",
    "accessibility_audit": "llama3.1",
}

DEFAULT_FALLBACK_MODEL = "qwen3.5:397b-cloud"

# Per-agent default timeout (seconds) when LLM_TIMEOUT not set; blog uses longer for draft/revision
AGENT_DEFAULT_TIMEOUTS: dict[str, float] = {
    "blog": 300.0,
}

# ---------------------------------------------------------------------------
# Resolvers (env + agent defaults)
# ---------------------------------------------------------------------------

def resolve_provider() -> str:
    """Return effective LLM provider: 'dummy' or 'ollama' (default)."""
    return _env(ENV_LLM_PROVIDER, ENV_LLM_PROVIDER_SW, "ollama").lower().strip()


def resolve_model(agent_key: Optional[str] = None) -> str:
    """
    Resolve model name: LLM_MODEL_<agent_key>, then LLM_MODEL, then AGENT_DEFAULT_MODELS[agent_key], then fallback.
    """
    if agent_key:
        per_agent = os.environ.get(f"LLM_MODEL_{agent_key}") or os.environ.get(f"SW_LLM_MODEL_{agent_key}")
        if per_agent:
            return per_agent.strip()
    global_model = _env(ENV_LLM_MODEL, ENV_LLM_MODEL_SW, "")
    if global_model:
        return global_model
    if agent_key and agent_key in AGENT_DEFAULT_MODELS:
        return AGENT_DEFAULT_MODELS[agent_key]
    return DEFAULT_FALLBACK_MODEL


def resolve_base_url() -> str:
    """Return Ollama base URL (default https://ollama.com for Ollama Cloud)."""
    return _env(ENV_LLM_BASE_URL, ENV_LLM_BASE_URL_SW, "https://ollama.com").rstrip("/")


def resolve_timeout(agent_key: Optional[str] = None) -> float:
    """Return timeout in seconds. Env override, then per-agent default (e.g. blog 300), else 120."""
    raw = os.environ.get(ENV_LLM_TIMEOUT) or os.environ.get(ENV_LLM_TIMEOUT_SW)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if agent_key and agent_key in AGENT_DEFAULT_TIMEOUTS:
        return AGENT_DEFAULT_TIMEOUTS[agent_key]
    return 120.0


def resolve_context_size_for_model(model: str) -> Optional[int]:
    """
    Resolve context size (tokens) for a model: env LLM_CONTEXT_SIZE (global override),
    then KNOWN_MODEL_CONTEXT[model], else None (caller may use /api/show or default).
    """
    raw = os.environ.get(ENV_LLM_CONTEXT_SIZE) or os.environ.get(ENV_LLM_CONTEXT_SIZE_SW)
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
