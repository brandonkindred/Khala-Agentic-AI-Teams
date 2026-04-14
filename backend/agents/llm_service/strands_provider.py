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

_model_cache: dict[tuple[str, str], LLMClientModel] = {}
_cache_lock = threading.Lock()


def get_strands_model(agent_key: Optional[str] = None) -> LLMClientModel:
    """Return a cached Strands-compatible model backed by the centralized LLM service.

    Model resolution follows the same rules as ``llm_service.factory.get_client``:
    ``LLM_MODEL_<agent_key>`` → ``LLM_MODEL`` → ``AGENT_DEFAULT_MODELS[agent_key]`` → fallback.

    The returned ``LLMClientModel`` wraps ``OllamaLLMClient`` which provides full
    retry-with-exponential-backoff for transient LLM errors (500s, connection resets,
    timeouts, 429 rate limits).

    Args:
        agent_key: Optional agent identifier for per-agent model overrides.

    Returns:
        A configured ``LLMClientModel`` instance backed by the centralized LLM client.
    """
    from . import config as llm_config

    model_id = llm_config.resolve_model(agent_key)
    base_url = llm_config.resolve_base_url()
    cache_key = (model_id, base_url)

    with _cache_lock:
        if cache_key not in _model_cache:
            backing_client = get_client(agent_key)
            _model_cache[cache_key] = LLMClientModel(
                backing_client,
                agent_key=agent_key,
                model_id=model_id,
            )
            logger.info(
                "Strands LLMClientModel created: model_id=%s, host=%s, agent_key=%s",
                model_id,
                base_url,
                agent_key,
            )

        return _model_cache[cache_key]


def _clear_strands_model_cache_for_testing() -> None:
    """Clear the Strands model cache. For use in tests only."""
    with _cache_lock:
        _model_cache.clear()
