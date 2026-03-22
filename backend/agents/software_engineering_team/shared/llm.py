"""
Thin LLM wrapper for software engineering team.

All provider logic and config live in llm_service. This module re-exports from llm_service
and adds complete_json_with_continuation (delegates to client; Ollama handles truncation in llm_service).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

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

    Delegates to client.complete_json. Ollama client (llm_service) now performs
    continuation internally on truncation; other clients may raise LLMTruncatedError.
    max_continuation_cycles and task_id are ignored when using Ollama.
    """
    return client.complete_json(prompt, temperature=temperature)


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
]
