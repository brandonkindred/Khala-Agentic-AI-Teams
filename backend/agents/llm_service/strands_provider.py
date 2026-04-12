"""Strands ModelProvider adapter — wraps llm_service config around strands.models.ollama.OllamaModel.

Teams obtain a Strands-compatible model via ``get_strands_model(agent_key)`` and pass it
to ``strands.Agent(model=...)``. The factory reuses llm_service's config resolution
(env vars, per-agent defaults, known context sizes) so model selection stays in one place.

The adapter uses Strands' native OllamaModel which handles streaming, tool calling,
retries, and conversation management natively — no double-looping through llm_service's
tool_loop or compaction.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from strands.models.ollama import OllamaModel

from . import config as llm_config

logger = logging.getLogger(__name__)

_model_cache: dict[tuple[str, str], OllamaModel] = {}
_cache_lock = threading.Lock()


def _resolve_ollama_auth_headers() -> dict[str, str]:
    """Return Authorization Bearer header for Ollama Cloud if an API key is set."""
    key = (
        (os.environ.get("OLLAMA_API_KEY") or "")
        or (os.environ.get(llm_config.ENV_LLM_OLLAMA_API_KEY) or "")
    ).strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def get_strands_model(agent_key: Optional[str] = None) -> OllamaModel:
    """Return a cached Strands OllamaModel for the given agent key.

    Model resolution follows the same rules as ``llm_service.factory.get_client``:
    ``LLM_MODEL_<agent_key>`` → ``LLM_MODEL`` → ``AGENT_DEFAULT_MODELS[agent_key]`` → fallback.

    Args:
        agent_key: Optional agent identifier for per-agent model overrides.

    Returns:
        A configured ``strands.models.ollama.OllamaModel`` instance.
    """
    model_id = llm_config.resolve_model(agent_key)
    base_url = llm_config.resolve_base_url()
    cache_key = (model_id, base_url)

    with _cache_lock:
        if cache_key not in _model_cache:
            headers = _resolve_ollama_auth_headers()
            client_args: dict = {}
            if headers:
                client_args["headers"] = headers

            ollama_kwargs: dict = {"model_id": model_id}

            # Apply max_tokens from config if set
            max_tokens_raw = os.environ.get(llm_config.ENV_LLM_MAX_TOKENS)
            if max_tokens_raw:
                try:
                    ollama_kwargs["max_tokens"] = int(max_tokens_raw)
                except ValueError:
                    pass

            _model_cache[cache_key] = OllamaModel(
                host=base_url,
                ollama_client_args=client_args if client_args else None,
                **ollama_kwargs,
            )
            logger.info(
                "Strands OllamaModel created: model_id=%s, host=%s",
                model_id,
                base_url,
            )

        return _model_cache[cache_key]


def _clear_strands_model_cache_for_testing() -> None:
    """Clear the Strands model cache. For use in tests only."""
    with _cache_lock:
        _model_cache.clear()
