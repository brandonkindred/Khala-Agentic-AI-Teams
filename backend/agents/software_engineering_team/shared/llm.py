"""
Thin LLM wrapper for software engineering team.

All provider logic and config live in llm_service. This module re-exports from llm_service
and adds SE-only helpers: complete_json_with_continuation (uses ResponseContinuator for
truncated Ollama responses).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from llm_service import (
    DummyLLMClient,
    LLMClient,
    LLMJsonParseError,
    LLMTruncatedError,
    OllamaLLMClient,
    call_llm_with_retries,
    extract_json_from_response,
    get_client,
    get_llm_config_summary,
)
from llm_service import (
    LLMError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMUnreachableAfterRetriesError,
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
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

    Uses client.complete_json; on LLMTruncatedError, uses ResponseContinuator for
    Ollama clients (base_url, model, timeout) and parses result with llm_service
    extract_json_from_response.
    """
    try:
        return client.complete_json(prompt, temperature=temperature)
    except LLMTruncatedError as e:
        logger.info(
            "Response truncated (%d chars). Attempting continuation (max %d cycles)",
            len(e.partial_content),
            max_continuation_cycles,
        )
        if not isinstance(client, OllamaLLMClient):
            raise
        from software_engineering_team.shared.continuation import (
            ContinuationResult,
            ResponseContinuator,
        )

        system_message = (
            "You are a strict JSON generator. Respond with a single valid JSON object only, "
            "no explanatory text, no Markdown, no code fences. "
            "If you use a code block, put only the JSON object inside it with no surrounding text."
        )
        continuator = ResponseContinuator(
            base_url=client.base_url,
            model=client.model,
            timeout=client.timeout,
            max_cycles=max_continuation_cycles,
        )
        result: ContinuationResult = continuator.attempt_continuation(
            original_prompt=prompt,
            partial_content=e.partial_content,
            system_prompt=system_message,
            json_mode=True,
            task_id=task_id or "sw_eng_json",
        )
        if result.success:
            logger.info(
                "Continuation succeeded after %d cycles (%d chars total)",
                result.cycles_used,
                len(result.content),
            )
            return extract_json_from_response(result.content)
        logger.warning(
            "Continuation exhausted after %d cycles (%d chars). Re-raising truncation error.",
            result.cycles_used,
            len(result.content),
        )
        raise LLMTruncatedError(
            f"Response still truncated after {result.cycles_used} continuation cycles",
            partial_content=result.content,
            finish_reason="length",
        )


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
