"""
Abstract interface and exceptions for the central LLM service.

All agent teams should depend on this interface and get_client(); they must not
construct provider-specific clients directly.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Exceptions (unified for all teams)
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base exception for LLM-related errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.cause = cause


class LLMRateLimitError(LLMError):
    """Raised when the LLM returns 429 Too Many Requests and retries are exhausted.

    ``retry_after_seconds`` optionally carries the value parsed from a provider
    ``Retry-After`` response header so the retry loop that catches this error can
    honor it (raising the wait to at least that long, never below the configured
    floor). ``None`` when no header was present or honoring is disabled.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        cause: Optional[Exception] = None,
        retry_after_seconds: Optional[float] = None,
    ):
        super().__init__(message, status_code=status_code, cause=cause)
        self.retry_after_seconds = retry_after_seconds


class LLMTemporaryError(LLMError):
    """Raised when the LLM returns 5xx or network errors and retries are exhausted."""


class LLMUnreachableAfterRetriesError(LLMTemporaryError):
    """Raised when the caller exhausted retries and could not reach the LLM. Orchestrator should pause job."""


class LLMSemanticExhaustionError(LLMTemporaryError):
    """Raised when the model produced no assistant content and no proof-of-change retry remains.

    A semantically exhausted call is one where the request transport succeeded
    (HTTP 200) but the model returned zero content — typically because it spent
    its whole generation on reasoning. Unlike transport faults, re-sending the
    identical payload is very unlikely to help, so the client performs at most
    one retry with reduced thinking and then raises this error as a terminal
    receipt instead of looping on the transient schedule.

    Subclasses ``LLMTemporaryError`` so existing pause/degrade handlers keep
    working: at the macro level the condition is temporary (a different prompt
    may succeed later), mirroring ``LLMUnreachableAfterRetriesError``.

    Preconditions:
        - ``attempts_used >= 1``; ``payload_fingerprint`` is a stable digest of
          the last request payload sent.
    Postconditions / Invariants:
        - ``failure_class`` is always ``"semantic_exhaustion"``.
        - ``retry_thinking_level`` is the reduced thinking value used on the
          proof-of-change retry, or ``None`` when no downgrade was available
          (thinking already off / at the lowest registered level).
        - ``content_bytes_seen`` is True iff any attempt produced at least one
          content byte.
    """

    failure_class = "semantic_exhaustion"

    def __init__(
        self,
        message: str,
        *,
        attempts_used: int,
        original_thinking_level: "bool | str | None",
        retry_thinking_level: "bool | str | None",
        content_bytes_seen: bool,
        payload_fingerprint: str,
        finish_reason: str = "",
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, cause=cause)
        self.attempts_used = attempts_used
        self.original_thinking_level = original_thinking_level
        self.retry_thinking_level = retry_thinking_level
        self.content_bytes_seen = content_bytes_seen
        self.payload_fingerprint = payload_fingerprint
        self.finish_reason = finish_reason


class LLMPermanentError(LLMError):
    """Raised for 4xx errors (except 429) or malformed responses. Do not retry."""


class LLMJsonParseError(LLMPermanentError):
    """Raised when LLM returned a 200 response but the content is not valid JSON."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "json_parse",
        response_preview: str = "",
        correction_attempts_used: int = 0,
    ):
        super().__init__(message)
        self.error_kind = error_kind
        self.response_preview = response_preview
        self.correction_attempts_used = correction_attempts_used


class LLMSchemaValidationError(LLMPermanentError):
    """Raised when the LLM returned valid JSON that fails Pydantic schema validation.

    Produced by the ``complete_validated`` helper after all corrective retries
    have been exhausted. Wraps the underlying ``pydantic.ValidationError`` so
    callers see a consistent ``LLMPermanentError`` subclass with the same
    ``correction_attempts_used`` shape as ``LLMJsonParseError``.
    """

    def __init__(
        self,
        message: str,
        *,
        response_preview: str = "",
        correction_attempts_used: int = 0,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, cause=cause)
        self.response_preview = response_preview
        self.correction_attempts_used = correction_attempts_used


class LLMTruncatedError(LLMError):
    """Raised when LLM response was truncated due to token limit (finish_reason=length)."""

    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        finish_reason: str = "length",
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.finish_reason = finish_reason


# Message used when Ollama 429 indicates weekly usage limit exceeded (for logging and job state)
OLLAMA_WEEKLY_LIMIT_MESSAGE = "Ollama LLM usage limit exceeded for week"


# ---------------------------------------------------------------------------
# LLMClient interface
# ---------------------------------------------------------------------------


class LLMClient(ABC):
    """
    Minimal abstraction around an LLM client.

    Implementations (Ollama, Dummy, future OpenAI/Anthropic) live in llm_service.clients.
    Agents obtain a client via get_client(agent_key?) and call complete_json / complete / get_max_context_tokens.
    """

    @abstractmethod
    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Run the model with the given prompt and return a JSON-decoded dict.

        Pass ``tools`` (OpenAI-compatible tool definitions) to enable function/tool calling.
        When the model invokes a tool, the returned dict has the key ``__tool_calls__`` whose
        value is a list of tool-call objects (id, type, function.name, function.arguments).
        Optional kwargs may include expected_keys, decomposition_hints for PA-style robust extraction.

        ``think`` controls chain-of-thought / reasoning mode: ``None`` (default)
        resolves to the platform default — the model's max registered thinking
        level when known; ``False`` disables; a string selects a specific level.
        """
        ...

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        """
        Run the model and return raw text.

        Override in implementations that support it. Default uses complete_json and extracts text.
        Pass ``tools`` for function/tool calling; tool-call responses are returned as JSON strings.

        ``think`` controls chain-of-thought / reasoning mode (see ``complete_json``).
        """
        result = self.complete_json(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            tools=tools,
            think=think,
        )
        if isinstance(result, dict) and len(result) == 1 and "text" in result:
            return str(result["text"])
        return json.dumps(result)

    def get_max_context_tokens(self) -> int:
        """
        Return the model's maximum context size in tokens.

        Used for context_sizing and chunking. Default 16384.
        Override in implementations that can query the model (e.g. Ollama).
        """
        return 16384

    # Alias for SE code that uses complete_text
    def complete_text(
        self, prompt: str, *, temperature: float = 0.0, think: "bool | str | None" = None
    ) -> str:
        """Alias for complete() for backward compatibility with SE team."""
        return self.complete(
            prompt, temperature=temperature, max_tokens=None, system_prompt=None, think=think
        )

    def chat(
        self,
        messages: list[Dict[str, Any]],
        *,
        response_format: str = "json",
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """One chat completion round, parameterized by ``response_format``.

        Returns one of:

        - ``{"__tool_calls__": [...]}`` when the model invokes tools, regardless
          of ``response_format`` (tool invocations always come back as a dict
          envelope).
        - A parsed ``Dict[str, Any]`` when ``response_format="json"`` (the
          default). The wire request includes ``response_format=json_object``
          when no tools are present, and the assistant content is parsed via
          ``_extract_json`` with markdown-fence and repair fallbacks.
        - A raw ``str`` of the assistant content when ``response_format="text"``.
          No forcing on the wire, no parsing — for conversational prose, the
          ``---DRAFT---`` marker pattern, template-based outputs, etc.

        Default implementation: not supported — override in Ollama / Dummy.
        """
        raise LLMPermanentError(f"{type(self).__name__} does not implement chat")
