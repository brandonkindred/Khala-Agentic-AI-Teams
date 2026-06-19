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

_model_cache: dict[tuple[str, str, str, Optional[str]], LLMClientModel] = {}
_cache_lock = threading.Lock()


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
    cached by ``(model_id, base_url, response_format)``.

    Args:
        agent_key: Optional agent identifier for per-agent model overrides.
        response_format: ``"json"`` (default) or ``"text"``.
        client: Optional pre-built ``LLMClient`` to wrap (bypasses cache).

    Returns:
        A configured ``LLMClientModel`` instance backed by the centralized LLM client.
    """
    from . import config as llm_config

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
            model_id=client_model or llm_config.resolve_model_for_provider(agent_key),
            response_format=response_format,
        )

    model_id = llm_config.resolve_model_for_provider(agent_key)

    # ``agent_key`` is part of the cache key so two agents that resolve to the
    # same model don't share one ``LLMClientModel`` (which would attribute every
    # later call to whichever agent constructed it first). Distinct keys get
    # distinct adapters, each backed by its own attribution-wrapped client.
    cache_key = (model_id, base_url, response_format, agent_key)

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

    Called by ``factory.clear_client_cache`` after a settings change: the cache key
    ``(model_id, base_url, response_format, agent_key)`` omits the API-key
    fingerprint, so without this an in-place key rotation would keep serving a
    Strands adapter whose backing client still holds the old key.

    Preconditions: none.
    Postconditions: the Strands model cache is empty afterward. Safe to call when
        nothing is cached.
    """
    with _cache_lock:
        _model_cache.clear()


def _clear_strands_model_cache_for_testing() -> None:
    """Clear the Strands model cache. For use in tests only."""
    clear_model_cache()
