"""Centralized LLM configuration resolution for the ``llm_service``.

This module is the single chokepoint for "what provider / model / key / context
window / thinking level is effective right now". Every setting follows one
resolution order:

    runtime (UI / Postgres) -> environment variable -> hard-coded default

Runtime values are the operator selections made in the LLM Provider settings UI
and persisted (Fernet-encrypted) in shared Postgres; they are read back here
**best-effort** via :mod:`llm_service.runtime_config` (the :func:`_runtime` helper
swallows any read/import failure and returns ``""``), so env vars are always a
working fallback and these resolvers never raise on a runtime-config problem.

Key exports:
    - ``resolve_*`` functions — the effective provider, model (per-provider and
      via the :func:`resolve_model_for_provider` chokepoint), API keys, base URL,
      timeout, context size, and ``max_tokens``; plus the thinking-level resolver
      (:func:`resolve_think_for_model`, which also applies the per-agent
      ``AGENT_DEFAULT_THINK`` pin).
    - ``ENV_*`` constants — the canonical ``LLM_*`` environment-variable names.
    - Model-option/suggestion constants (``CLAUDE_MODEL_SUGGESTIONS``,
      ``OLLAMA_MODEL_SUGGESTIONS``) and the known context-window / thinking-level
      tables.

Environment variables use the ``LLM_*`` prefix.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

# shared.env is a dependency-free standard-library-only leaf module, so importing
# it at module scope cannot create an import cycle (it imports nothing from here).
from shared.env import env_flag_enabled as _env_flag_enabled
from shared.env import parse_float as _parse_float

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable names (LLM_*)
# ---------------------------------------------------------------------------

ENV_LLM_PROVIDER = "LLM_PROVIDER"
ENV_LLM_MODEL = "LLM_MODEL"
ENV_LLM_BASE_URL = "LLM_BASE_URL"
ENV_LLM_TIMEOUT = "LLM_TIMEOUT"
ENV_LLM_CONTEXT_SIZE = "LLM_CONTEXT_SIZE"
ENV_LLM_MAX_OUTPUT_TOKENS = "LLM_MAX_OUTPUT_TOKENS"
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
# Multi-provider failover (see llm_service/provider_store.py + factory.FailoverLLMClient).
# When more than one provider is configured, a 429 on one provider hands off to the
# next instead of burning the slow LLM_RATE_LIMIT_* backoff in place. FAST_429 (default
# on) makes the failover-chain clients raise the 429 immediately (zero in-place
# rate-limit retries) so the hand-off isn't delayed by minutes. The *_WINDOW_S vars are
# fallback reset windows (session/weekly are fixed from the error; rate uses
# Retry-After when present).
ENV_LLM_FAILOVER_FAST_429 = "LLM_FAILOVER_FAST_429"
ENV_LLM_FAILOVER_RATE_WINDOW_S = "LLM_FAILOVER_RATE_WINDOW_S"
ENV_LLM_FAILOVER_SESSION_WINDOW_S = "LLM_FAILOVER_SESSION_WINDOW_S"
ENV_LLM_FAILOVER_WEEKLY_WINDOW_S = "LLM_FAILOVER_WEEKLY_WINDOW_S"
ENV_LLM_MAX_CONCURRENCY = "LLM_MAX_CONCURRENCY"
ENV_LLM_ENABLE_THINKING = "LLM_ENABLE_THINKING"
# No dedicated resolver: read directly by resolve_think_for_model below (it picks
# this level when the model registers it, else warns and falls back to the model's
# max level).
ENV_LLM_THINKING_LEVEL = "LLM_THINKING_LEVEL"
# No dedicated resolver: read directly by the Ollama client's downgrade-retry gate
# (clients/ollama.py, via env_flag_enabled) — a falsy value disables reducing the
# thinking level as the proof-of-change on a semantically-exhausted retry.
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
# Guards the check-then-add on the warn set so the "warn once per distinct model"
# intent holds under concurrent get_client()/get_strands_model() calls.
_warned_lock = threading.Lock()

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

# Curated Claude model ids surfaced as suggestions by the settings UI. Derived
# from KNOWN_CLAUDE_CONTEXT so the suggestion list and the context-window table are
# a single source of truth that cannot silently drift apart: every suggested model
# has a known context window. The model field also accepts free text.
CLAUDE_MODEL_SUGGESTIONS: list[str] = list(KNOWN_CLAUDE_CONTEXT.keys())

# Invariant: the default model must itself have a known context window (and thus
# appear in the suggestion list), so it never falls back to DEFAULT_CLAUDE_CONTEXT.
# An explicit raise (not a bare ``assert``) so the check survives ``python -O``.
if DEFAULT_CLAUDE_MODEL not in KNOWN_CLAUDE_CONTEXT:
    raise RuntimeError(
        f"DEFAULT_CLAUDE_MODEL {DEFAULT_CLAUDE_MODEL!r} must be a key in KNOWN_CLAUDE_CONTEXT"
    )

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

    Thin re-export of the canonical :func:`shared.env.env_flag_enabled` so there is
    one implementation; kept here for the many call sites that import it from
    ``llm_service.config``.

    Preconditions:
        - ``env_name`` is a non-empty environment variable name.
    Postconditions:
        - Returns False only for an explicit "false"/"0"/"no"
          (case-insensitive, whitespace-tolerant); unset or any other value
          means enabled. Never raises.
    """
    return _env_flag_enabled(env_name)


def thinking_enabled_by_default() -> bool:
    """Global thinking default: enabled unless LLM_ENABLE_THINKING is falsy.

    Postconditions:
        - Returns False only for explicit "false"/"0"/"no" (case-insensitive);
          unset or any other value means enabled.
    """
    return env_flag_enabled(ENV_LLM_ENABLE_THINKING)


def _resolve_agent_think_pin(model: str) -> "str | None":
    """Per-agent pinned thinking tier for the currently-attributed agent, or None.

    Some agents run a reasoning model in JSON mode where the model's top tier
    reliably produces content-free, reasoning-only turns; ``AGENT_DEFAULT_THINK``
    pins a reduced tier so their FIRST call opens the content channel. Resolving
    it here — the one chokepoint every provider path threads ``think`` through —
    pins the strands path and the direct-client (compaction) path identically,
    keyed off the agent bound on the attribution context.

    The pin is only a *default*: None is returned (leaving the model's normal
    resolution to run) when

      - no agent is attributed, or the attributed agent has no pin;
      - the operator disabled thinking (``LLM_ENABLE_THINKING``) or set a level
        override (``LLM_THINKING_LEVEL``) — global knobs outrank the pin;
      - ``model`` does not register the pinned level — pinning an effort level a
        model never declared would put an unvalidated guess on the wire (the
        reason unregistered models otherwise resolve to plain boolean think).

    Preconditions:
        - ``model`` is the resolved wire model id for the call.
    Postconditions:
        - Returns a level string registered for ``model``, or None. Reads the
          attribution context and the thinking env vars; never raises.
    """
    from .attribution import current_attribution

    level = resolve_agent_default_think(current_attribution().agent_key)
    if level is None:
        return None
    # Operator global knobs outrank the per-agent default.
    if not thinking_enabled_by_default():
        return None
    if (os.environ.get(ENV_LLM_THINKING_LEVEL) or "").strip():
        return None
    # Never send a level the resolved wire model does not register.
    if level not in (KNOWN_MODEL_THINKING_LEVELS.get(model) or ()):
        return None
    return level


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
        - When ``think`` is None, a per-agent pin (``AGENT_DEFAULT_THINK`` for the
          attributed agent, via :func:`_resolve_agent_think_pin`) takes effect
          *before* the model-default tier — but ranked below the operator's
          ``LLM_ENABLE_THINKING``/``LLM_THINKING_LEVEL`` knobs and only for a
          level the model registers.
    """
    if isinstance(think, str):
        return think
    if think is False:
        return False
    if think is None:
        pinned = _resolve_agent_think_pin(model)
        if pinned is not None:
            return pinned
        if not thinking_enabled_by_default():
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


# ---------------------------------------------------------------------------
# Per-agent default model when LLM_MODEL_<agent_key> and LLM_MODEL are unset
# ---------------------------------------------------------------------------

AGENT_DEFAULT_MODELS: dict[str, str] = {
    "backend": "kimi-k2.7-code:cloud",
    "frontend": "kimi-k2.7-code:cloud",
    "code_review": "kimi-k2.7-code:cloud",
    # Narrower, bounded code-review sub-passes (false-positive verify, narrative
    # synthesis) rather than the open-ended main review. deepseek-v4-pro:cloud's
    # reasoning_effort wire mapping collapses "low"/"medium" onto the same "high"
    # tier as code_review (see KNOWN_MODEL_THINKING_LEVELS below), so a thinking-tier
    # pin alone cannot make this genuinely lighter; qwen3.5:9b-mlx is this codebase's
    # established smaller/faster model tier (already used for soc2,
    # accessibility_audit) and has no registered thinking levels of its own, so no
    # AGENT_DEFAULT_THINK entry applies here.
    "code_review_verify": "deepseek-v4-flash:cloud",
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
    "soc2": "deepseek-v4-flash:cloud",
    "blog": "deepseek-v4-pro:cloud",
    "personal_assistant": "llama3.2",
    "accessibility_audit": "deepseek-v4-flash:cloud",
    "strategy_ideation": "deepseek-v4-pro:cloud",
    "signal_intelligence": "deepseek-v4-pro:cloud",
    "deepthought": "deepseek-v4-pro:cloud",
}

DEFAULT_FALLBACK_MODEL = "deepseek-v4-pro:cloud"

# ---------------------------------------------------------------------------
# Per-agent default thinking level
# ---------------------------------------------------------------------------
# Some agents run a reasoning model in forced JSON mode, where the model's top
# "max" reasoning tier can burn the whole turn in the reasoning channel and emit
# no assistant content ("reasoning-only" turns → semantic exhaustion). For those
# agents we pin a reduced default thinking level so the FIRST call already runs
# at a tier that reliably opens the content channel, instead of relying on the
# client's post-hoc downgrade retry after a doomed max-tier call. Only agents
# listed here override the model's platform-default (max) tier; every other agent
# is unaffected.
AGENT_DEFAULT_THINK: dict[str, str] = {
    # code_review runs deepseek-v4-pro:cloud in JSON mode; at reasoning_effort
    # "max" it frequently returns reasoning-only turns (semantic exhaustion), so
    # it defaults to the reduced "high" tier — DeepSeek's other true wire tier —
    # which opens the content channel far more reliably.
    "code_review": "high",
}


def resolve_agent_default_think(agent_key: "str | None") -> "str | None":
    """Return the pinned default thinking level for ``agent_key``, or None.

    Preconditions:
        - ``agent_key`` is an agent key string or None.
    Postconditions:
        - Returns the ``AGENT_DEFAULT_THINK`` level for the key when one is
          registered, else None (the caller then falls back to the model's
          platform-default tier). Pure function: no env reads, never raises.
    """
    if not agent_key:
        return None
    return AGENT_DEFAULT_THINK.get(agent_key)


# Curated Ollama model ids surfaced as suggestions by the settings UI. Centralized
# here (rather than inline in the unified_api route) so the UI suggestion list and
# the rest of the model config share one home and can't silently drift, mirroring
# CLAUDE_MODEL_SUGGESTIONS above. The model field also accepts free text, so this is
# a suggestion list, not a closed set.
OLLAMA_MODEL_SUGGESTIONS: list[str] = [
    "deepseek-v4-pro:cloud",
    "qwen3-coder:480b-cloud",
    "qwen3.5:9b-mlx",
    "deepseek-v4-flash:cloud",
    "llama3.2",
]

# ---------------------------------------------------------------------------
# Resolvers (env + agent defaults)
# ---------------------------------------------------------------------------


def _runtime(key: str) -> str:
    """Return a runtime-config value (UI-managed, Postgres-backed), or ``""``.

    Lazily delegates to :mod:`llm_service.runtime_config`. Any failure (Postgres
    disabled, shared.postgres absent, read error) yields ``""`` so env-var
    resolution remains the fallback. Never raises.
    """
    try:
        from . import runtime_config

        return runtime_config.get_runtime(key)
    except Exception as e:  # noqa: BLE001 - runtime config is best-effort
        # Best-effort fallback to env resolution, but leave a breadcrumb: a silent
        # empty return would otherwise hide a real runtime_config bug (import
        # failure, Postgres misconfig) behind "config just isn't set".
        logger.debug("runtime config read failed for key %s: %s", key, e)
        return ""


def _runtime_key(attr: str) -> Optional[str]:
    """Resolve a ``runtime_config.KEY_*`` constant by attribute name, or ``None``.

    Reads the canonical key string from :mod:`llm_service.runtime_config` (rather
    than duplicating the literal here, which would let the two drift). Guards the
    lazy import so a resolver honors its "Never raises" postcondition even when
    runtime_config can't be imported — the caller then skips the runtime lookup and
    falls through to env/default. Returning ``None`` (not ``""``) keeps a missing
    key from being passed into :func:`_runtime` (whose ``get_runtime`` rejects an
    unknown key).

    Preconditions: ``attr`` names a ``KEY_*`` constant on ``runtime_config``.
    Postconditions: returns the key string, or ``None`` when runtime_config is
        unavailable. Never raises.
    """
    try:
        from . import runtime_config as _rc

        return getattr(_rc, attr)
    except Exception as e:  # noqa: BLE001 - runtime config is best-effort
        logger.debug("runtime config key lookup failed for %s: %s", attr, e)
        return None


def resolve_provider() -> str:
    """Return effective LLM provider: 'dummy', 'claude', or 'ollama' (default).

    Resolution order: runtime config (UI) -> ``LLM_PROVIDER`` env -> ``ollama``.
    The ``anthropic`` alias normalizes to ``claude``.

    Postconditions: returns a lowercase, stripped provider id; ``"anthropic"``
        maps to ``"claude"``. Never raises.
    """
    key = _runtime_key("KEY_PROVIDER")
    runtime = _runtime(key) if key else ""
    raw = (runtime or os.environ.get(ENV_LLM_PROVIDER) or "ollama").lower().strip()
    if raw in ("anthropic", "claude"):
        return "claude"
    return raw


def resolve_max_output_tokens() -> int:
    """Return the configured output-token cap from ``LLM_MAX_OUTPUT_TOKENS``, or 0 if unset.

    Centralizes the ``LLM_MAX_OUTPUT_TOKENS`` env lookup so provider clients don't read
    ``os.environ`` directly (mirrors the other resolvers here) — used by
    :meth:`OllamaLLMClient._resolve_max_tokens`. A missing,
    non-integer, or non-positive value yields ``0`` — the caller's "unset"
    sentinel — so a client falls through to its own provider default rather than a
    1-token cap that would truncate every call.

    Postconditions: returns an int ``>= 0``. Never raises.
    """
    raw = os.environ.get(ENV_LLM_MAX_OUTPUT_TOKENS)
    if not raw:
        return 0
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 0
    return val if val > 0 else 0


# Fallback reset windows (seconds). Session/weekly Cloud caps ignore Retry-After
# and use these fixed windows from the error time; rate uses Retry-After when
# present, else the short rate window (~5m). Weekly defaults to 24h because
# Ollama Cloud weekly 429 bodies do not include a reset timestamp.
_DEFAULT_FAILOVER_RATE_WINDOW_S = 300.0
_DEFAULT_FAILOVER_SESSION_WINDOW_S = 65 * 60.0
_DEFAULT_FAILOVER_WEEKLY_WINDOW_S = 24 * 3600.0


def failover_fast_429_enabled() -> bool:
    """True when failover-chain clients should fail fast on 429 (default on).

    Postconditions: returns the boolean value of ``LLM_FAILOVER_FAST_429`` (default
        True when unset/blank). When True the factory builds failover-chain provider
        clients with a zero in-place rate-limit retry budget so a 429 hands off to
        the next provider immediately instead of sleeping minutes. Never raises.
    """
    # ``env_flag_enabled`` is already a default-on toggle (unset/blank -> True; only
    # an explicit false/0/no -> False) reading the env once.
    return _env_flag_enabled(ENV_LLM_FAILOVER_FAST_429)


def _resolve_positive_window(env_name: str, default: float) -> float:
    """Return a positive seconds value from ``env_name``, else ``default``.

    Postconditions: returns ``> 0``; a missing/unparseable/non-positive env yields
        ``default``. Never raises.
    """
    val = _parse_float(env_name, default)
    return val if val > 0 else default


def failover_rate_window_seconds() -> float:
    """Fallback reset window for a rate 429 with no Retry-After (default 5m).

    Postconditions: returns ``> 0`` seconds. Never raises.
    """
    return _resolve_positive_window(ENV_LLM_FAILOVER_RATE_WINDOW_S, _DEFAULT_FAILOVER_RATE_WINDOW_S)


def failover_session_window_seconds() -> float:
    """Fixed reset window for a session-limit 429 (default 65m).

    Session/weekly classifications ignore ``Retry-After``; this window is measured
    from the error time.

    Postconditions: returns ``> 0`` seconds. Never raises.
    """
    return _resolve_positive_window(
        ENV_LLM_FAILOVER_SESSION_WINDOW_S, _DEFAULT_FAILOVER_SESSION_WINDOW_S
    )


def failover_weekly_window_seconds() -> float:
    """Fixed reset window for a weekly-limit 429 (default 24h).

    Session/weekly classifications ignore ``Retry-After``; this window is measured
    from the error time (Ollama Cloud weekly bodies omit a reset timestamp).

    Postconditions: returns ``> 0`` seconds. Never raises.
    """
    return _resolve_positive_window(
        ENV_LLM_FAILOVER_WEEKLY_WINDOW_S, _DEFAULT_FAILOVER_WEEKLY_WINDOW_S
    )


def _looks_like_claude_model(model: str) -> bool:
    """Return True when ``model`` looks like an Anthropic/Claude model id.

    Heuristic guard so a cross-provider model id (e.g. an Ollama model left in the
    shared ``LLM_MODEL`` env, which defaults to a non-Claude model) is never sent to
    the Anthropic API.

    Matching rules (case-insensitive, applied to the stripped value):
        - a ``claude``, ``anthropic.``, or ``anthropic/`` prefix (the latter two are
          the Bedrock/Vertex gateway forms) -> match; or
        - the token ``anthropic``, ``claude``, ``fable``, or ``mythos`` appears
          anywhere in the id -> match.
    Anything else (including the empty string) -> no match.

    Postconditions: returns a bool; never raises. ``""`` -> False.
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.startswith(("claude", "anthropic.", "anthropic/")):
        return True
    return any(token in m for token in ("anthropic", "claude", "fable", "mythos"))


def resolve_claude_model(agent_key: Optional[str] = None) -> str:
    """Resolve the Claude model id for ``agent_key``.

    Ordered sources: ``LLM_MODEL_<agent_key>`` (per-agent env) -> runtime model
    (the LLM Provider UI, stored under the Claude-specific ``KEY_CLAUDE_MODEL``) ->
    ``LLM_MODEL`` (global env), falling back to ``DEFAULT_CLAUDE_MODEL``. The two
    **env** candidates are shared with the Ollama path (the global ``LLM_MODEL``
    defaults to a non-Claude model), so each is validated with
    :func:`_looks_like_claude_model` and skipped with a warning when it isn't a
    Claude id — it must never reach the Anthropic API. The **runtime** value is
    Claude-specific (the operator chose it explicitly for this provider), so it is
    trusted as-is: a free-typed custom/gateway Claude model is honored even when it
    doesn't match the heuristic. The runtime value is ranked above the global env so
    a UI selection wins; a per-agent ``LLM_MODEL_<agent_key>`` pin outranks it but —
    being a shared env candidate — is honored only when it passes
    :func:`_looks_like_claude_model`. The Ollama
    ``AGENT_DEFAULT_MODELS`` table is deliberately never consulted.

    Postconditions: returns a non-empty model id string.
    """
    key = _runtime_key("KEY_CLAUDE_MODEL")
    runtime_model = _runtime(key).strip() if key else ""

    # (candidate, trusted) in priority order. Env candidates are shared across
    # providers so they are heuristic-validated; the provider-specific runtime
    # selection is trusted without filtering.
    candidates: list[tuple[str, bool]] = []
    if agent_key:
        candidates.append(((os.environ.get(f"{ENV_LLM_MODEL}_{agent_key}") or "").strip(), False))
    candidates.append((runtime_model, True))
    candidates.append(((os.environ.get(ENV_LLM_MODEL) or "").strip(), False))

    for candidate, trusted in candidates:
        if not candidate:
            continue
        if trusted or _looks_like_claude_model(candidate):
            return candidate
        with _warned_lock:
            should_warn = candidate not in _warned_non_claude_models
            if should_warn:
                _warned_non_claude_models.add(candidate)
        if should_warn:
            if agent_key:
                logger.warning(
                    "Ignoring non-Claude model %r for the Claude provider for agent %s; "
                    "falling back to the next candidate. Set a Claude model in the LLM "
                    "Provider settings or LLM_MODEL.",
                    candidate,
                    agent_key,
                )
            else:
                logger.warning(
                    "Ignoring non-Claude model %r for the Claude provider; falling back "
                    "to the next candidate. Set a Claude model in the LLM Provider "
                    "settings or LLM_MODEL.",
                    candidate,
                )
    return DEFAULT_CLAUDE_MODEL


def resolve_claude_api_key() -> str:
    """Return the Claude API key: runtime -> ``LLM_CLAUDE_API_KEY`` -> ``ANTHROPIC_API_KEY``.

    Postconditions: returns the first non-empty source stripped of whitespace, or
        ``""`` when none is configured (the client then surfaces a clear auth error
        on first call). Never raises.
    """
    key = _runtime_key("KEY_CLAUDE_API_KEY")
    runtime = _runtime(key).strip() if key else ""
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
    key = _runtime_key("KEY_OLLAMA_API_KEY")
    runtime = _runtime(key).strip() if key else ""
    if runtime:
        return runtime
    return (
        os.environ.get("OLLAMA_API_KEY") or os.environ.get(ENV_LLM_OLLAMA_API_KEY) or ""
    ).strip()


def resolve_model(agent_key: Optional[str] = None) -> str:
    """Resolve the Ollama model id for ``agent_key``.

    Ordered sources: ``LLM_MODEL_<agent_key>`` (per-agent env) -> runtime model
    (the LLM Provider settings UI, stored under the Ollama-specific
    ``KEY_OLLAMA_MODEL``) -> ``LLM_MODEL`` (global env) ->
    ``AGENT_DEFAULT_MODELS[agent_key]`` -> ``DEFAULT_FALLBACK_MODEL``. The runtime
    (UI) value is ranked above the global env so a model chosen in the settings
    page is honored — consistent with ``resolve_provider`` / ``resolve_base_url`` /
    ``resolve_claude_model`` (whose Ollama counterpart this is). The runtime model
    is provider-specific, so no cross-provider filtering is needed here.

    Postconditions: returns a non-empty model id string. Never raises.
    """
    if agent_key:
        per_agent = (os.environ.get(f"{ENV_LLM_MODEL}_{agent_key}") or "").strip()
        if per_agent:
            return per_agent
    key = _runtime_key("KEY_OLLAMA_MODEL")
    runtime_model = _runtime(key).strip() if key else ""
    if runtime_model:
        return runtime_model
    global_model = (os.environ.get(ENV_LLM_MODEL) or "").strip()
    if global_model:
        return global_model
    if agent_key and agent_key in AGENT_DEFAULT_MODELS:
        return AGENT_DEFAULT_MODELS[agent_key]
    return DEFAULT_FALLBACK_MODEL


def resolve_model_for_provider(
    agent_key: Optional[str] = None, provider: Optional[str] = None
) -> str:
    """Resolve the model id for the *active* provider.

    Single chokepoint for the "which model id under the current provider"
    decision so the factory, the Strands adapter, and the config summary share one
    rule instead of each re-deriving the ``provider == 'claude'`` branch: Claude ->
    :func:`resolve_claude_model`; everything else (ollama/dummy) ->
    :func:`resolve_model`.

    Preconditions: ``provider`` is the already-resolved active provider id, or
        ``None`` to resolve it here (a caller that already has it passes it to
        avoid a redundant :func:`resolve_provider` lock acquisition).
    Postconditions: returns a non-empty model id appropriate for the
        active provider. Never raises.
    """
    active = provider or resolve_provider()
    if active == "claude":
        return resolve_claude_model(agent_key)
    return resolve_model(agent_key)


def resolve_base_url() -> str:
    """Return Ollama base URL (runtime -> ``LLM_BASE_URL`` env -> Ollama Cloud).

    The runtime value lets the settings UI toggle between local Ollama
    (``http://host:11434``) and Ollama Cloud (``https://ollama.com``).

    Postconditions: returns a non-empty URL with no trailing slash. A
        whitespace-only runtime or env candidate is treated as unset and falls
        through to the next candidate. Never raises.
    """
    key = _runtime_key("KEY_OLLAMA_BASE_URL")
    runtime = (_runtime(key) or "").strip() if key else ""
    env_url = (os.environ.get(ENV_LLM_BASE_URL) or "").strip()
    return (runtime or env_url or "https://ollama.com").rstrip("/")


def resolve_timeout(agent_key: Optional[str] = None) -> float:
    """Return timeout in seconds (default 3600 — 60 min).

    All LLM calls use streaming, so the timeout covers the full streamed response.
    Override with LLM_TIMEOUT if needed.
    """
    # A non-positive timeout would make every streamed call fail instantly;
    # fall back to the default rather than honor a degenerate override.
    value = _parse_float(ENV_LLM_TIMEOUT, 3600.0)
    return value if value > 0 else 3600.0


def resolve_context_size_for_model(model: str) -> Optional[int]:
    """
    Resolve context size (tokens) for a model.

    Order: env ``LLM_CONTEXT_SIZE`` (global override, clamped to a minimum of
    2048), then ``KNOWN_MODEL_CONTEXT[model]``, else ``None`` (caller may use
    ``/api/show`` or default).

    Postconditions: when an env override is a valid int, returns ``>= 2048``;
        otherwise returns the known-model value or ``None``. Never raises.
    """
    # Not expressible via shared.env.parse_int: an unset *or* invalid override must
    # fall through to the model-specific KNOWN_MODEL_CONTEXT value (an Optional[int]),
    # not a fixed default, and a valid override is clamped up to a 2048 floor.
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
            f"provider={provider}, model={resolve_model_for_provider(None, provider)}, "
            f"base_url={resolve_base_url()}"
        )
    if provider == "claude":
        return f"provider={provider}, model={resolve_model_for_provider(None, provider)}"
    return f"provider={provider}"
