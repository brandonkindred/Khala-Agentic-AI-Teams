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
# Rate-limit (HTTP 429) backoff — a deliberately SLOW schedule distinct from the
# transient (5xx/network) LLM_BACKOFF_* schedule above. A 429 means the provider
# budget is exhausted and will not reset in seconds, so the first retry waits
# minutes, not seconds. See llm_service/backoff.py.
ENV_LLM_RATE_LIMIT_MAX_RETRIES = "LLM_RATE_LIMIT_MAX_RETRIES"
ENV_LLM_RATE_LIMIT_BACKOFF_INITIAL = "LLM_RATE_LIMIT_BACKOFF_INITIAL"
ENV_LLM_RATE_LIMIT_BACKOFF_MAX = "LLM_RATE_LIMIT_BACKOFF_MAX"
ENV_LLM_RATE_LIMIT_HONOR_RETRY_AFTER = "LLM_RATE_LIMIT_HONOR_RETRY_AFTER"
ENV_LLM_MAX_CONCURRENCY = "LLM_MAX_CONCURRENCY"
ENV_LLM_ENABLE_THINKING = "LLM_ENABLE_THINKING"
ENV_LLM_THINKING_LEVEL = "LLM_THINKING_LEVEL"
ENV_LLM_THINKING_DOWNGRADE_RETRY = "LLM_THINKING_DOWNGRADE_RETRY"
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
    "deepseek-v4-pro:cloud": 1000000,
}

# ---------------------------------------------------------------------------
# Known model thinking levels — ordered lowest → highest; resolution picks the
# last entry as the platform's "max thinking" default. Models not listed here
# only support boolean think on the wire.
# ---------------------------------------------------------------------------

KNOWN_MODEL_THINKING_LEVELS: dict[str, tuple[str, ...]] = {
    # DeepSeek's thinking-mode docs list reasoning_effort "high" and "max"
    # (the true maximum), with compatibility mapping low/medium → high and
    # xhigh → max — so all four registered names are accepted on the wire
    # and "max" is the platform default for this model.
    "deepseek-v4-pro:cloud": ("low", "medium", "high", "max"),
}


def env_flag_enabled(env_name: str) -> bool:
    """Shared parser for default-on boolean env toggles.

    Preconditions:
        - ``env_name`` is a non-empty environment variable name.
    Postconditions:
        - Returns False only for an explicit "false"/"0"/"no"
          (case-insensitive, whitespace-tolerant); unset or any other value
          means enabled. Never raises.
    """
    return (os.environ.get(env_name) or "").strip().lower() not in ("false", "0", "no")


def thinking_enabled_by_default() -> bool:
    """Global thinking default: enabled unless LLM_ENABLE_THINKING is falsy.

    Postconditions:
        - Returns False only for explicit "false"/"0"/"no" (case-insensitive);
          unset or any other value means enabled.
    """
    return env_flag_enabled(ENV_LLM_ENABLE_THINKING)


def resolve_think_for_model(model: str, think: "bool | str | None") -> "bool | str":
    """Resolve a caller's think request into the wire value for ``model``.

    Preconditions:
        - ``think`` is None, a bool, or a thinking-level string.
    Postconditions:
        - Explicit values win: a string passes through verbatim and False
          stays False. True — or None when the global default is enabled —
          upgrades to the model's highest registered thinking level;
          LLM_THINKING_LEVEL overrides that when it names one of the model's
          registered levels, and garbage values fall back to the max.
          Models with no registered levels resolve to plain True (level
          strings would be rejected on the wire). None with the global
          default disabled resolves to False.
    """
    if isinstance(think, str):
        return think
    if think is False:
        return False
    if think is None and not thinking_enabled_by_default():
        return False
    levels = KNOWN_MODEL_THINKING_LEVELS.get(model)
    if not levels:
        return True
    override = (os.environ.get(ENV_LLM_THINKING_LEVEL) or "").strip().lower()
    if override:
        if override in levels:
            return override
        logger.warning(
            "LLM_THINKING_LEVEL=%r is not a known level for %s %r; using max level %r",
            override,
            model,
            levels,
            levels[-1],
        )
    return levels[-1]


def downgrade_think(model: str, think: "bool | str") -> "bool | str | None":
    """Next-lower thinking setting for ``model``, or None when no proof of change exists.

    Used by the proof-of-change retry for semantically exhausted calls: the
    retry payload must provably differ from the original, and reducing the
    thinking level is the chosen change agent.

    Preconditions:
        - ``think`` is a resolved wire value (bool or level string, never None).
    Postconditions:
        - ``True`` -> ``False``; ``False`` -> ``None`` (already off — nothing
          left to change).
        - A level string registered in ``KNOWN_MODEL_THINKING_LEVELS[model]``
          -> the previous (lower) level, or ``None`` when already the lowest.
        - A level string not registered for the model -> ``False`` (disabling
          reasoning is the only provable change available).
        - Pure function: no env reads, never raises.
    """
    if think is True:
        return False
    if think is False:
        return None
    levels = KNOWN_MODEL_THINKING_LEVELS.get(model) or ()
    if think in levels:
        idx = levels.index(think)
        return levels[idx - 1] if idx > 0 else None
    return False


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
