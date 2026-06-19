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
# Claude / Anthropic API key. ``LLM_CLAUDE_API_KEY`` is the Khala-namespaced name;
# ``ANTHROPIC_API_KEY`` is the SDK's own convention and is honored as a fallback.
ENV_LLM_CLAUDE_API_KEY = "LLM_CLAUDE_API_KEY"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

# Default cap for max_tokens (many APIs limit output to 32K even when context is 256K)
DEFAULT_MAX_OUTPUT_TOKENS = 32768

# Default Claude model when no per-agent / global / runtime model is configured.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"

# Candidates already warned about in resolve_claude_model, so a non-Claude
# LLM_MODEL under the Claude provider (e.g. the default deepseek model) logs once
# per distinct value instead of on every get_client()/get_strands_model() call.
_warned_non_claude_models: set[str] = set()

# ---------------------------------------------------------------------------
# Known Claude context windows (input tokens). Used by ClaudeLLMClient when
# LLM_CONTEXT_SIZE is unset. The current Opus/Sonnet/Fable family ships a 1M
# window; Haiku 4.5 is 200K. Unlisted models fall back to a conservative default.
# ---------------------------------------------------------------------------

KNOWN_CLAUDE_CONTEXT: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
}

DEFAULT_CLAUDE_CONTEXT = 200_000

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


def _runtime(key: str) -> str:
    """Return a runtime-config value (UI-managed, Postgres-backed), or ``""``.

    Lazily delegates to :mod:`llm_service.runtime_config`. Any failure (Postgres
    disabled, shared_postgres absent, read error) yields ``""`` so env-var
    resolution remains the fallback. Never raises.
    """
    try:
        from . import runtime_config

        return runtime_config.get_runtime(key)
    except Exception:  # noqa: BLE001 - runtime config is best-effort
        return ""


def resolve_provider() -> str:
    """Return effective LLM provider: 'dummy', 'claude', or 'ollama' (default).

    Resolution order: runtime config (UI) -> ``LLM_PROVIDER`` env -> ``ollama``.
    The ``anthropic`` alias normalizes to ``claude``.

    Postconditions: returns a lowercase, stripped provider id; ``"anthropic"``
        maps to ``"claude"``. Never raises.
    """
    from . import runtime_config as _rc

    raw = (
        (_runtime(_rc.KEY_PROVIDER) or os.environ.get(ENV_LLM_PROVIDER) or "ollama").lower().strip()
    )
    if raw in ("anthropic", "claude"):
        return "claude"
    return raw


def _looks_like_claude_model(model: str) -> bool:
    """Return True when ``model`` looks like an Anthropic/Claude model id.

    Heuristic guard so a cross-provider model id (e.g. an Ollama model left in
    ``LLM_MODEL`` or a stale runtime model from before a provider switch) is never
    sent to the Anthropic API. Matches the Claude/Fable/Mythos families and the
    Bedrock/Vertex-style ``anthropic.``/``claude-`` prefixes.

    Postconditions: returns a bool; never raises. ``""`` -> False.
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.startswith(("claude", "anthropic.", "anthropic/")):
        return True
    return any(token in m for token in ("claude", "fable", "mythos"))


def resolve_claude_model(agent_key: Optional[str] = None) -> str:
    """Resolve the Claude model id for ``agent_key``.

    Ordered, **provider-validated** sources: ``LLM_MODEL_<agent_key>`` (per-agent
    env) -> runtime model (the LLM Provider UI) -> ``LLM_MODEL`` (global env). Each
    candidate is accepted only if :func:`_looks_like_claude_model`; a non-Claude
    id (e.g. an Ollama model still in ``LLM_MODEL``, or a stale runtime model left
    over from a provider switch) is skipped with a warning so it never reaches the
    Anthropic API. Falls back to ``DEFAULT_CLAUDE_MODEL``. The Ollama
    ``AGENT_DEFAULT_MODELS`` table is deliberately never consulted.

    Note the runtime (UI) value is ranked above the global ``LLM_MODEL`` env, so
    a UI-selected Claude model is honored (consistent with ``resolve_provider`` /
    ``resolve_base_url``); per-agent env pinning still wins when set.

    Postconditions: returns a non-empty Claude-looking model id string.
    """
    from . import runtime_config as _rc

    candidates: list[str] = []
    if agent_key:
        candidates.append((os.environ.get(f"{ENV_LLM_MODEL}_{agent_key}") or "").strip())
    candidates.append(_runtime(_rc.KEY_MODEL).strip())
    candidates.append((os.environ.get(ENV_LLM_MODEL) or "").strip())

    for candidate in candidates:
        if not candidate:
            continue
        if _looks_like_claude_model(candidate):
            return candidate
        if candidate not in _warned_non_claude_models:
            _warned_non_claude_models.add(candidate)
            logger.warning(
                "Ignoring non-Claude model %r for the Claude provider; using default %s. "
                "Set a Claude model in the LLM Provider settings or LLM_MODEL.",
                candidate,
                DEFAULT_CLAUDE_MODEL,
            )
    return DEFAULT_CLAUDE_MODEL


def resolve_claude_api_key() -> str:
    """Return the Claude API key: runtime -> ``LLM_CLAUDE_API_KEY`` -> ``ANTHROPIC_API_KEY``.

    Postconditions: returns the first non-empty source stripped of whitespace, or
        ``""`` when none is configured (the client then surfaces a clear auth error
        on first call). Never raises.
    """
    from . import runtime_config as _rc

    runtime = _runtime(_rc.KEY_CLAUDE_API_KEY).strip()
    if runtime:
        return runtime
    return (
        os.environ.get(ENV_LLM_CLAUDE_API_KEY) or os.environ.get(ENV_ANTHROPIC_API_KEY) or ""
    ).strip()


def resolve_claude_context_size(model: str) -> int:
    """Return the input-token context window for a Claude ``model``.

    Order: ``LLM_CONTEXT_SIZE`` env (global override) -> ``KNOWN_CLAUDE_CONTEXT`` ->
    ``DEFAULT_CLAUDE_CONTEXT`` (200K).

    Postconditions: returns an int ``>= 2048``.
    """
    raw = os.environ.get(ENV_LLM_CONTEXT_SIZE)
    if raw:
        try:
            return max(2048, int(raw))
        except ValueError:
            pass
    return KNOWN_CLAUDE_CONTEXT.get(model, DEFAULT_CLAUDE_CONTEXT)


def resolve_ollama_api_key() -> str:
    """Return the Ollama Cloud API key: runtime -> ``OLLAMA_API_KEY`` -> ``LLM_OLLAMA_API_KEY``.

    Postconditions: returns the first non-empty source stripped, else ``""`` (local
        Ollama needs no key). Never raises.
    """
    from . import runtime_config as _rc

    runtime = _runtime(_rc.KEY_OLLAMA_API_KEY).strip()
    if runtime:
        return runtime
    return (
        os.environ.get("OLLAMA_API_KEY") or os.environ.get(ENV_LLM_OLLAMA_API_KEY) or ""
    ).strip()


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


def resolve_model_for_provider(agent_key: Optional[str] = None) -> str:
    """Resolve the model id for the *active* provider.

    Single chokepoint for the "which model id under the current provider"
    decision so the factory, the Strands adapter, and the config summary share one
    rule instead of each re-deriving the ``provider == 'claude'`` branch: Claude ->
    :func:`resolve_claude_model`; everything else (ollama/dummy) ->
    :func:`resolve_model`.

    Postconditions: returns a non-empty model id appropriate for
        :func:`resolve_provider`. Never raises.
    """
    if resolve_provider() == "claude":
        return resolve_claude_model(agent_key)
    return resolve_model(agent_key)


def resolve_base_url() -> str:
    """Return Ollama base URL (runtime -> ``LLM_BASE_URL`` env -> Ollama Cloud).

    The runtime value lets the settings UI toggle between local Ollama
    (``http://host:11434``) and Ollama Cloud (``https://ollama.com``).
    """
    from . import runtime_config as _rc

    raw = (
        _runtime(_rc.KEY_OLLAMA_BASE_URL)
        or os.environ.get(ENV_LLM_BASE_URL)
        or "https://ollama.com"
    )
    return raw.strip().rstrip("/")


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
    """Return a short summary of effective provider and model for logging.

    Never includes API keys — only provider, model, and (for Ollama) base URL.
    """
    provider = resolve_provider()
    if provider == "ollama":
        return (
            f"provider={provider}, model={resolve_model_for_provider(None)}, "
            f"base_url={resolve_base_url()}"
        )
    if provider == "claude":
        return f"provider={provider}, model={resolve_model_for_provider(None)}"
    return f"provider={provider}"
