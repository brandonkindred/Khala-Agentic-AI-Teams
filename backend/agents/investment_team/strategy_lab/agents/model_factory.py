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
from typing import Optional

from llm_service.config import resolve_base_url, resolve_model, resolve_provider, resolve_timeout

logger = logging.getLogger(__name__)


def _resolve_strands_timeout(agent_key: str) -> float:
    """Resolve the transport-level timeout (seconds) for the strands client.

    ``STRATEGY_LAB_LLM_TIMEOUT`` takes precedence; otherwise falls back to the
    platform-wide ``resolve_timeout`` (which honours ``LLM_TIMEOUT``).

    Preconditions: ``agent_key`` is a non-empty model key.
    Postconditions: returns a positive float — garbage env values fall back.
    """
    raw = os.environ.get("STRATEGY_LAB_LLM_TIMEOUT")
    if raw is not None and raw.strip() != "":
        try:
            return float(raw)
        except ValueError:
            pass
    return resolve_timeout(agent_key)


def _construct_with_optional_timeout(model_cls, timeout: float, **kwargs):
    """Construct ``model_cls`` forwarding a transport timeout if the SDK accepts it.

    The installed strands ``OllamaModel`` / ``BedrockModel`` signatures vary by
    version (``timeout=`` vs ``client_args={"timeout": ...}`` vs neither). We try
    each shape in turn and fall back to constructing without a timeout so a
    signature mismatch degrades to "envelope wall-clock guard only" rather than
    breaking agent construction.

    Preconditions: ``model_cls`` is a strands Model class; ``timeout > 0``.
    Postconditions: returns a constructed model instance.
    """
    for attempt_kwargs in (
        {**kwargs, "timeout": timeout},
        {**kwargs, "client_args": {"timeout": timeout}},
    ):
        try:
            return model_cls(**attempt_kwargs)
        except TypeError:
            continue
    logger.warning(
        "Strands model %s did not accept a transport timeout; relying on the "
        "envelope wall-clock guard only.",
        getattr(model_cls, "__name__", model_cls),
    )
    return model_cls(**kwargs)


def get_strands_model(agent_key: str = "strategy_ideation", *, timeout: Optional[float] = None):
    """Return a Strands ``Model`` instance for the given agent key.

    The Strands SDK defaults to BedrockModel when ``model`` is a string.
    This factory explicitly constructs the correct provider so that Bedrock
    is only used when ``LLM_PROVIDER=bedrock`` is set.

    ``timeout`` (seconds) is the transport-level read timeout forwarded to the
    underlying client — the only mechanism that actually cancels a hung HTTP
    call. Defaults to ``STRATEGY_LAB_LLM_TIMEOUT`` / ``resolve_timeout``. The
    Strategy Lab LLM envelope adds a secondary wall-clock guard on top.
    """
    provider = resolve_provider()
    model_id = resolve_model(agent_key)
    base_url = resolve_base_url()
    if timeout is None:
        timeout = _resolve_strands_timeout(agent_key)

    if provider == "bedrock":
        from strands.models import BedrockModel

        logger.info("Strands model: Bedrock model_id=%s timeout=%.0fs", model_id, timeout)
        return _construct_with_optional_timeout(BedrockModel, timeout, model_id=model_id)

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

    logger.info(
        "Strands model: Ollama model_id=%s host=%s cloud=%s timeout=%.0fs",
        model_id,
        host,
        bool(api_key),
        timeout,
    )
    return _construct_with_optional_timeout(OllamaModel, timeout, host=host, model_id=model_id)
