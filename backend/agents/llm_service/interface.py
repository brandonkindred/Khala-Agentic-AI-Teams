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
    """Raised when the LLM returns 429 Too Many Requests and retries are exhausted."""


class LLMTemporaryError(LLMError):
    """Raised when the LLM returns 5xx or network errors and retries are exhausted."""


class LLMUnreachableAfterRetriesError(LLMTemporaryError):
    """Raised when the caller exhausted retries and could not reach the LLM. Orchestrator should pause job."""


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
    ):
        super().__init__(message)
        self.error_kind = error_kind
        self.response_preview = response_preview


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
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Run the model with the given prompt and return a JSON-decoded dict.

        Pass ``tools`` (OpenAI-compatible tool definitions) to enable function/tool calling.
        When the model invokes a tool, the returned dict has the key ``__tool_calls__`` whose
        value is a list of tool-call objects (id, type, function.name, function.arguments).
        Optional kwargs may include expected_keys, decomposition_hints for PA-style robust extraction.

        ``think`` controls chain-of-thought / reasoning mode (default ``False``).
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
        think: bool = False,
    ) -> str:
        """
        Run the model and return raw text.

        Override in implementations that support it. Default uses complete_json and extracts text.
        Pass ``tools`` for function/tool calling; tool-call responses are returned as JSON strings.

        ``think`` controls chain-of-thought / reasoning mode (default ``False``).
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
    def complete_text(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        """Alias for complete() for backward compatibility with SE team."""
        return self.complete(
            prompt, temperature=temperature, max_tokens=None, system_prompt=None, think=think
        )
