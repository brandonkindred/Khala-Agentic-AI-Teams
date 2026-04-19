"""Resolve a Strands Model instance from environment configuration.

Priority:
  1. Ollama Cloud  (OLLAMA_API_KEY set)
  2. Ollama local  (LLM_BASE_URL points to a local server)
  3. Bedrock       (LLM_PROVIDER=bedrock)
  4. Error         (nothing configured)

Uses the existing ``llm_service.config`` resolvers so that all LLM_* env vars
are respected consistently with the rest of the platform.
"""

from __future__ import annotations

import logging
import os

from llm_service.config import resolve_base_url, resolve_model, resolve_provider

logger = logging.getLogger(__name__)


def get_strands_model(agent_key: str = "strategy_ideation"):
    """Return a Strands ``Model`` instance for the given agent key.

    The Strands SDK defaults to BedrockModel when ``model`` is a string.
    This factory explicitly constructs the correct provider so that Bedrock
    is only used when ``LLM_PROVIDER=bedrock`` is set.
    """
    provider = resolve_provider()
    model_id = resolve_model(agent_key)
    base_url = resolve_base_url()

    if provider == "bedrock":
        from strands.models import BedrockModel

        logger.info("Strands model: Bedrock model_id=%s", model_id)
        return BedrockModel(model_id=model_id)

    if provider == "dummy":
        raise ValueError(
            "LLM_PROVIDER=dummy is not supported for Strands agents. "
            "Set LLM_PROVIDER=ollama or LLM_PROVIDER=bedrock."
        )

    # Provider is "ollama" (the default).
    # The ``ollama`` Python package auto-reads OLLAMA_API_KEY for Bearer auth
    # and OLLAMA_HOST for the host URL, but we also honour LLM_BASE_URL and
    # LLM_OLLAMA_API_KEY from the existing llm_service config.
    from strands.models import OllamaModel

    host = os.environ.get("OLLAMA_HOST") or base_url
    api_key = (
        os.environ.get("OLLAMA_API_KEY") or os.environ.get("LLM_OLLAMA_API_KEY") or ""
    ).strip()

    if not api_key and "ollama.com" in host:
        raise ValueError(
            "Ollama Cloud requires an API key. Set OLLAMA_API_KEY (or "
            "LLM_OLLAMA_API_KEY), or point LLM_BASE_URL / OLLAMA_HOST "
            "to a local Ollama server (e.g. http://localhost:11434)."
        )

    logger.info("Strands model: Ollama model_id=%s host=%s cloud=%s", model_id, host, bool(api_key))
    return OllamaModel(host=host, model_id=model_id)
