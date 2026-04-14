"""
Thin LLM wrapper for software engineering team.

All provider logic and config live in llm_service. This module re-exports from llm_service
and adds complete_json_with_continuation (delegates to Strands Agent).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from strands import Agent

from llm_service import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    DummyLLMClient,
    LLMClient,
    LLMError,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
    LLMUnreachableAfterRetriesError,
    OllamaLLMClient,
    call_llm_with_retries,
    extract_json_from_response,
    get_client,
    get_llm_config_summary,
    get_strands_model,
)

logger = logging.getLogger(__name__)

# Backward-compat aliases so existing "from software_engineering_team.shared.llm import get_llm_for_agent" still works
get_llm_for_agent = get_client
get_llm_client = get_client


def complete_json_with_continuation(
    client: LLMClient,
    prompt: str,
    *,
    temperature: float = 0.0,
    max_continuation_cycles: int = 5,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Complete JSON request with automatic continuation on truncation.

    Uses a Strands Agent for the LLM call. Parses the agent's text output as JSON.
    max_continuation_cycles and task_id are accepted for backward compatibility but ignored.

    When *client* implements the Strands ``Model`` protocol (e.g. a
    ``DummyLLMClient`` in tests), it is used directly as the agent model so
    that tests do not need a live LLM server.
    """
    from strands.models.model import Model as _StrandsModel

    if client is not None and isinstance(client, _StrandsModel):
        model = client
    else:
        model = get_strands_model(task_id)
    agent = Agent(
        model=model,
        system_prompt="You are a helpful assistant. Always respond with valid JSON only.",
        callback_handler=None,
    )
    result = agent(prompt)
    raw = str(result).strip()
    # Try bare json.loads first; fall back to extract_json_from_response for
    # responses wrapped in markdown fences or prefixed with explanatory text.
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return extract_json_from_response(raw)


__all__ = [
    "DummyLLMClient",
    "LLMClient",
    "LLMError",
    "LLMJsonParseError",
    "LLMPermanentError",
    "LLMRateLimitError",
    "LLMTemporaryError",
    "LLMTruncatedError",
    "LLMUnreachableAfterRetriesError",
    "OLLAMA_WEEKLY_LIMIT_MESSAGE",
    "OllamaLLMClient",
    "call_llm_with_retries",
    "complete_json_with_continuation",
    "extract_json_from_response",
    "get_client",
    "get_llm_config_summary",
    "get_llm_for_agent",
    "get_llm_client",
    "get_strands_model",
]
