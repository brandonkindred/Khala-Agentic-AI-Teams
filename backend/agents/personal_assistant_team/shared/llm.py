"""
Thin LLM layer for Personal Assistant team.

All provider logic lives in llm_service. This module re-exports from llm_service
and adds PA-specific JSONExtractionFailure (wrapping LLMJsonParseError) and a
client wrapper that re-raises central LLM errors as JSONExtractionFailure for
backward compatibility.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from llm_service import (
    LLMClient,
    LLMError,
    LLMJsonParseError,
    LLMTruncatedError,
    get_client,
)


class JSONExtractionFailure(LLMJsonParseError):
    """
    Raised when JSON extraction fails. Subclasses llm_service.LLMJsonParseError.
    PA agents can catch this for backward compatibility; central client raises LLMJsonParseError.
    """

    def __init__(
        self,
        message: str,
        *,
        original_prompt: str = "",
        attempts_made: int = 1,
        continuation_attempts: int = 0,
        decomposition_attempts: int = 0,
        raw_responses: Optional[List[str]] = None,
        recovery_suggestions: Optional[List[str]] = None,
        error_kind: str = "json_parse",
        response_preview: str = "",
    ):
        super().__init__(message, error_kind=error_kind, response_preview=response_preview)
        self.original_prompt = original_prompt
        self.attempts_made = attempts_made
        self.continuation_attempts = continuation_attempts
        self.decomposition_attempts = decomposition_attempts
        self.raw_responses = raw_responses or []
        self.recovery_suggestions = recovery_suggestions or []


class _PALLMClientWrapper(LLMClient):
    """Wraps central LLMClient and re-raises LLMJsonParseError as JSONExtractionFailure."""

    def __init__(self, inner: LLMClient):
        self._inner = inner

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            return self._inner.complete_json(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                **kwargs,
            )
        except LLMJsonParseError as e:
            raise JSONExtractionFailure(
                str(e),
                original_prompt=prompt,
                attempts_made=1,
                continuation_attempts=0,
                decomposition_attempts=0,
                raw_responses=[getattr(e, "response_preview", "") or ""],
                recovery_suggestions=[
                    "Check that the prompt asks for valid JSON.",
                    "Try simplifying the request or breaking it into smaller parts.",
                ],
                response_preview=getattr(e, "response_preview", ""),
            ) from e

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        return self._inner.complete(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
            system_prompt=system_prompt,
        )

    def get_max_context_tokens(self) -> int:
        return self._inner.get_max_context_tokens()


def get_llm_client_with_pa_exceptions(agent_key: Optional[str] = None) -> LLMClient:
    """Return a client that re-raises LLMJsonParseError as JSONExtractionFailure (for PA agents)."""
    return _PALLMClientWrapper(get_client(agent_key))


def get_llm_client(agent_key: Optional[str] = None) -> LLMClient:
    """Return PA wrapper around central client (re-raises LLMJsonParseError as JSONExtractionFailure)."""
    return get_llm_client_with_pa_exceptions(agent_key)
