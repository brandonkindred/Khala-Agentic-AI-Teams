"""
Factory for obtaining an LLM client by agent key or default.

Resolves provider and model from config (env + per-agent overrides + default table).
Caches Ollama clients by (model, base_url, timeout). Thread-safe.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Union

from . import config as llm_config
from .interface import LLMClient
from .clients import DummyLLMClient, OllamaLLMClient

logger = logging.getLogger(__name__)

_client_cache: dict[tuple[str, str, float], OllamaLLMClient] = {}
_cache_lock = threading.Lock()


def get_client(agent_key: Optional[str] = None) -> Union[DummyLLMClient, OllamaLLMClient]:
    """
    Return an LLM client for the given agent key or default.

    Model resolution: LLM_MODEL_<agent_key>, then LLM_MODEL, then AGENT_DEFAULT_MODELS[agent_key], then fallback.
    When LLM_PROVIDER=dummy, returns DummyLLMClient. Otherwise returns OllamaLLMClient (cached by model, base_url, timeout).
    """
    provider = llm_config.resolve_provider()
    if provider == "dummy":
        return DummyLLMClient()

    model = llm_config.resolve_model(agent_key)
    base_url = llm_config.resolve_base_url()
    timeout = llm_config.resolve_timeout(agent_key)
    cache_key = (model, base_url, timeout)

    with _cache_lock:
        if cache_key not in _client_cache:
            _client_cache[cache_key] = OllamaLLMClient(model=model, base_url=base_url, timeout=timeout)
        client = _client_cache[cache_key]

    if agent_key is None:
        logger.info("LLM config: %s", llm_config.get_llm_config_summary())
    return client


def _clear_client_cache_for_testing() -> None:
    """Clear the Ollama client cache. For use in tests only."""
    with _cache_lock:
        _client_cache.clear()
