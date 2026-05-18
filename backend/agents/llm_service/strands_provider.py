"""Strands ModelProvider adapter — wraps llm_service's OllamaLLMClient as a Strands Model.

Teams obtain a Strands-compatible model via ``get_strands_model(agent_key)`` and pass it
to ``strands.Agent(model=...)``. Under the hood, this returns a ``LLMClientModel`` that
delegates to the centralized ``OllamaLLMClient`` — which means every Strands agent
automatically inherits:

- **Retry with exponential backoff** for transient errors (500s, connection resets, timeouts)
- **Rate-limit handling** (429s) with backoff
- **Concurrency limiting** via global semaphore
- **Per-agent model routing** (``LLM_MODEL_<agent_key>``, agent defaults, etc.)
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .factory import get_client
from .strands_adapter import LLMClientModel

logger = logging.getLogger(__name__)

_model_cache: dict[tuple[str, str, str], LLMClientModel] = {}
_cache_lock = threading.Lock()


def get_strands_model(
    agent_key: Optional[str] = None,
    *,
    response_format: str = "json",
) -> LLMClientModel:
    """Return a cached Strands-compatible model backed by the centralized LLM service.

    Model resolution follows the same rules as ``llm_service.factory.get_client``:
    ``LLM_MODEL_<agent_key>`` → ``LLM_MODEL`` → ``AGENT_DEFAULT_MODELS[agent_key]`` → fallback.

    The returned ``LLMClientModel`` wraps ``OllamaLLMClient`` which provides full
    retry-with-exponential-backoff for transient LLM errors (500s, connection resets,
    timeouts, 429 rate limits).

    ``response_format`` is forwarded to ``LLMClientModel``: ``"json"`` (default)
    forces JSON output on the wire via ``chat_json_round`` — the safe default
    for Strands agents that ask for JSON in their system prompt and then
    ``json.loads`` the assistant content. ``"text"`` opts into ``chat_round``
    for free-form prose, used by conversational agents (e.g. branding
    assistant). The cache is keyed by ``(model_id, base_url, response_format)``
    so callers asking for different formats get distinct cached instances.

    Args:
        agent_key: Optional agent identifier for per-agent model overrides.
        response_format: ``"json"`` (default) or ``"text"``.

    Returns:
        A configured ``LLMClientModel`` instance backed by the centralized LLM client.
    """
    from . import config as llm_config

    model_id = llm_config.resolve_model(agent_key)
    base_url = llm_config.resolve_base_url()
    cache_key = (model_id, base_url, response_format)

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


def _clear_strands_model_cache_for_testing() -> None:
    """Clear the Strands model cache. For use in tests only."""
    with _cache_lock:
        _model_cache.clear()
