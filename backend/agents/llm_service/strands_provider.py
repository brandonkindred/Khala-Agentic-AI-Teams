"""Strands ModelProvider adapter — wraps llm_service's OllamaLLMClient as a Strands Model.

Teams obtain a Strands-compatible model via ``get_strands_model(agent_key)`` and pass it
to ``strands.Agent(model=...)``. Under the hood, this returns a ``LLMClientModel`` that
delegates to the centralized ``OllamaLLMClient`` — which means every Strands agent
automatically inherits:

- **Retry with exponential backoff** for transient errors (500s, connection resets, timeouts)
- **Rate-limit handling** (429s) on the slow, dedicated rate-limit backoff
  schedule (`LLM_RATE_LIMIT_*`, 300s initial by default), separate from the fast
  transient schedule
- **Concurrency limiting** via global semaphore
- **Per-agent model routing** (``LLM_MODEL_<agent_key>``, agent defaults, etc.)
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .factory import get_client
from .interface import LLMClient
from .strands_adapter import LLMClientModel

logger = logging.getLogger(__name__)

# Key: (provider, model_id, base_url, response_format, agent_key, key_fingerprint).
_model_cache: dict[tuple[str, str, str, str, Optional[str], str], LLMClientModel] = {}
_cache_lock = threading.Lock()


def _active_provider_key_fingerprint(provider: Optional[str] = None) -> str:
    """Fingerprint of the active provider's API key, for cache-key invalidation.

    Returns a stable short digest of the Claude or Ollama API key (whichever the
    resolved provider uses), or ``"no-key"`` when none is configured. Including
    this in the model cache key makes an in-place API-key rotation rebuild the
    adapter even in containers that pick the new key up only via the runtime-config
    TTL — those never call :func:`clear_model_cache` (it fires solely in the PUT
    handler's process), so without the fingerprint a rotated key would keep being
    served by a model wrapping a client built with the old key. Mirrors the
    factory's Claude client cache, which already keys on the fingerprint.

    Preconditions: ``provider`` is the already-resolved active provider id, or
        ``None`` to resolve it here (callers on the hot path pass it to avoid a
        redundant ``resolve_provider`` lock acquisition).
    Postconditions: returns a non-empty string; never raises.
    """
    from . import config as llm_config
    from .util import sha256_fingerprint

    if provider is None:
        provider = llm_config.resolve_provider()
    if provider in ("claude", "anthropic"):
        api_key = llm_config.resolve_claude_api_key()
    elif provider == "ollama":
        api_key = llm_config.resolve_ollama_api_key()
    else:
        api_key = ""
    return sha256_fingerprint(api_key) if api_key else "no-key"


def get_strands_model(
    agent_key: Optional[str] = None,
    *,
    response_format: str = "json",
    client: Optional[LLMClient] = None,
) -> LLMClientModel:
    """Return a Strands-compatible model backed by the centralized LLM service.

    Model resolution follows the same rules as ``llm_service.factory.get_client``:
    ``LLM_MODEL_<agent_key>`` → ``LLM_MODEL`` → ``AGENT_DEFAULT_MODELS[agent_key]`` → fallback.

    The returned ``LLMClientModel`` wraps ``OllamaLLMClient`` (or whichever
    provider ``LLM_PROVIDER`` selects) and inherits retry-with-exponential-
    backoff for transient LLM errors (500s, connection resets, timeouts) plus a
    separate slow backoff for 429 rate limits (``LLM_RATE_LIMIT_*``, 300s initial
    by default), concurrency limiting, and per-agent model routing.

    ``response_format`` is forwarded to ``LLMClientModel``: ``"json"`` (default)
    forces JSON output on the wire via ``chat(response_format="json")`` — the safe default
    for Strands agents that ask for JSON in their system prompt and then
    ``json.loads`` the assistant content. ``"text"`` opts into ``chat(response_format="text")``
    for free-form prose (conversational agents, template-based phases).

    ``client`` lets callers wrap a specific ``LLMClient`` instance (e.g. a
    pre-configured ``OllamaLLMClient``, a ``DummyLLMClient`` for tests, or a
    fresh client for a different model). When set, the cache is bypassed —
    each call returns a fresh ``LLMClientModel`` over the provided client,
    matching the adapter-side factory's contract. When omitted, results are
    cached by ``(model_id, base_url, response_format, agent_key, api_key_fingerprint)``.

    Args:
        agent_key: Optional agent identifier for per-agent model overrides.
        response_format: ``"json"`` (default) or ``"text"``.
        client: Optional pre-built ``LLMClient`` to wrap (bypasses cache).

    Returns:
        A configured ``LLMClientModel`` instance backed by the centralized LLM client.
    """
    from . import config as llm_config

    # Resolve the active provider ONCE and thread it through the model-id and
    # fingerprint helpers below. They would each re-resolve it otherwise, so a
    # single cached lookup took the runtime-config lock for the provider key three
    # times; this collapses that to one.
    provider = llm_config.resolve_provider()

    # Provider-aware model id: under LLM_PROVIDER=claude the Strands model_id /
    # cache key must use the Claude model, not the Ollama-resolved one, or
    # telemetry and the cache identity are tagged with the wrong model name.
    # ``resolve_model_for_provider`` is the shared chokepoint for that decision.
    base_url = llm_config.resolve_base_url()

    # Caller-supplied client bypasses the cache — they own the lifecycle and
    # may be passing distinct clients (different models, different timeouts,
    # tests) that share the same (model_id, base_url) key with the default
    # path. Caching here would alias them.
    if client is not None:
        # Prefer the actual model exposed by the client for telemetry / logs;
        # falling back to the env-resolved default would mis-tag the model
        # whenever the caller wraps a client whose ``.model`` differs from
        # ``LLM_MODEL``/``LLM_MODEL_<agent_key>``.
        client_model = getattr(client, "model", None)
        return LLMClientModel(
            client,
            agent_key=agent_key,
            model_id=client_model or llm_config.resolve_model_for_provider(agent_key, provider=provider),
            response_format=response_format,
        )

    model_id = llm_config.resolve_model_for_provider(agent_key, provider=provider)

    # ``agent_key`` is part of the cache key so two agents that resolve to the
    # same model don't share one ``LLMClientModel`` (which would attribute every
    # later call to whichever agent constructed it first). Distinct keys get
    # distinct adapters, each backed by its own attribution-wrapped client.
    # The active provider AND its API-key fingerprint are part of the key so a
    # provider switch or an in-place key rotation rebuilds the adapter even in
    # containers that refresh config only via the runtime-config TTL (which never
    # call clear_model_cache). Without the provider, an ollama<->claude/dummy switch
    # that happened to resolve the same model_id and key fingerprint (e.g. both
    # providers keyless with a shared LLM_MODEL) would keep serving a model wrapping
    # the wrong provider's client; without the fingerprint, a rotated key would keep
    # being served by a model wrapping a stale client.
    key_fingerprint = _active_provider_key_fingerprint(provider)
    cache_key = (provider, model_id, base_url, response_format, agent_key, key_fingerprint)

    with _cache_lock:
        if cache_key not in _model_cache:
            backing_client = get_client(agent_key)
            _model_cache[cache_key] = LLMClientModel(
                backing_client,
                agent_key=agent_key,
                model_id=model_id,
                response_format=response_format,
            )
            logger.info(
                "Strands LLMClientModel created: model_id=%s, host=%s, agent_key=%s, response_format=%s",
                model_id,
                base_url,
                agent_key,
                response_format,
            )

        return _model_cache[cache_key]


def clear_model_cache() -> None:
    """Drop all cached Strands models so the next call rebuilds against new config.

    Called by ``factory.clear_client_cache`` after a settings change. The cache key
    already includes the active provider and its API-key fingerprint, so most
    settings changes are invalidated by the key itself; this explicit clear is the
    belt-and-suspenders path that runs in the PUT handler's own process, dropping
    every cached adapter immediately rather than waiting for the next differing key.

    Preconditions: none.
    Postconditions: the Strands model cache is empty afterward. Safe to call when
        nothing is cached.
    """
    with _cache_lock:
        _model_cache.clear()


def _clear_strands_model_cache_for_testing() -> None:
    """Clear the Strands model cache. For use in tests only."""
    clear_model_cache()
