"""
Thin LLM wrapper for software engineering team.

All provider logic and config live in llm_service. This module re-exports from llm_service
and adds complete_json_with_continuation (delegates to Strands Agent).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Union

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
from llm_service.strands_model import resolve_strands_model

DEFAULT_JSON_SYSTEM_PROMPT = "You are a helpful assistant. Always respond with valid JSON only."

logger = logging.getLogger(__name__)


def complete_json_with_continuation(
    client: LLMClient,
    prompt: str,
    *,
    system_prompt: str = DEFAULT_JSON_SYSTEM_PROMPT,
    temperature: float = 0.0,
    think: Optional[Union[bool, str]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Complete JSON request with automatic continuation on truncation.

    Uses a Strands Agent for the LLM call. Parses the agent's text output as JSON.

    Preconditions:
        ``prompt`` is a non-empty string. ``client`` is a Strands ``Model``, an
        ``LLMClient``, or ``None`` (see :func:`resolve_strands_model`).
    Postconditions:
        Returns the parsed JSON response as a dict. Responses wrapped in
        markdown fences, prefixed with explanatory prose, or containing minor
        formatting defects (e.g. trailing commas) are recovered via
        ``extract_json_from_response`` before giving up. Raises
        ``LLMJsonParseError`` (a subclass of ``LLMPermanentError``) only when
        recovery also fails.
    """
    model = resolve_strands_model(client, agent_key=task_id, get_strands_model_fn=get_strands_model)
    agent = Agent(
        model=model,
        system_prompt=system_prompt,
        callback_handler=None,
    )
    invocation_kwargs: Dict[str, Any] = {"temperature": temperature}
    if think is not None:
        invocation_kwargs["think"] = think
    result = agent(prompt, **invocation_kwargs)
    raw = str(result).strip()
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
    "get_strands_model",
]
