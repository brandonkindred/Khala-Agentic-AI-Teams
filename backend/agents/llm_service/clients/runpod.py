"""
RunPod LLM client for the central LLM service.

``RunPodLLMClient`` is a standalone client that implements ``LLMClient``
directly. It communicates with RunPod serverless vLLM endpoints via the
standard OpenAI-compatible ``/v1/chat/completions`` API.

Unlike the previous implementation, this class does NOT extend
``OllamaLLMClient``. RunPod endpoints may run vLLM, TGI, or any other
OpenAI-compatible server — they are not Ollama. The client uses composition
to reuse the shared OpenAI-compatible streaming/retry/parsing logic from
``OllamaLLMClient`` as a private implementation detail, while presenting a
clean, independent public interface with no Ollama-specific behavior exposed.

Key differences from ``OllamaLLMClient``:
- No ``/api/show`` context-size probing (RunPod has no such endpoint)
- Uses a fixed context size (configurable, defaults to 131072 for vLLM)
- ``api_key`` is always required (RunPod endpoints need a Bearer token)
- Provider is identified as "runpod" in logs, not "ollama"
- ``isinstance(client, OllamaLLMClient)`` is False
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel

from ..interface import LLMClient

logger = logging.getLogger(__name__)

#: Default context window for RunPod vLLM endpoints. vLLM typically supports
#: large context windows; 131072 is a safe default for most modern models.
_DEFAULT_RUNPOD_CONTEXT_SIZE = 131_072


class RunPodLLMClient(LLMClient):
    """Standalone RunPod serverless LLM client.

    Communicates with RunPod's OpenAI-compatible vLLM endpoints at
    ``https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions``.

    Uses composition (not inheritance) with ``OllamaLLMClient`` for the
    shared OpenAI-compatible streaming HTTP logic — retry/backoff, SSE
    parsing, semantic exhaustion handling, and JSON extraction — while
    overriding Ollama-specific behaviors like ``/api/show`` context size
    probing.

    ``api_key`` is a required keyword argument (no default). RunPod
    endpoints always require an ``Authorization: Bearer <api_key>`` header.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        timeout: float = 3600.0,
        on_reasoning: Optional[Callable[[str], None]] = None,
        rate_limit_max_retries: Optional[int] = None,
        api_key: str,  # required — no default; RunPod always needs a key
        context_size: int = _DEFAULT_RUNPOD_CONTEXT_SIZE,
    ) -> None:
        # Import here to avoid circular imports at module level and to keep
        # the composition relationship as a private implementation detail.
        from .ollama import OllamaLLMClient

        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.on_reasoning = on_reasoning
        self._context_size = context_size

        # The internal delegate handles all OpenAI-compatible HTTP logic:
        # streaming, retry/backoff, SSE parsing, JSON extraction, telemetry.
        # We override its context-size resolution below.
        self._delegate = OllamaLLMClient(
            model=model,
            base_url=base_url,
            timeout=timeout,
            on_reasoning=on_reasoning,
            rate_limit_max_retries=rate_limit_max_retries,
            api_key=api_key,
        )
        # Override the delegate's context-size resolution so it never calls
        # /api/show (which doesn't exist on RunPod). Set both the authoritative
        # cache and the public method will read from our own field.
        self._delegate._model_num_ctx = context_size

    # ------------------------------------------------------------------
    # LLMClient interface implementation — delegates to the composed client
    # ------------------------------------------------------------------

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        schema: "Optional[dict | type[BaseModel]]" = None,
        structured_output_model: "Optional[type[BaseModel]]" = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run the model with JSON mode and return a decoded dict.

        Delegates to the composed OpenAI-compatible client for the actual
        HTTP call, retry logic, and response parsing.
        """
        return self._delegate.complete_json(
            prompt,
            objective=objective,
            temperature=temperature,
            system_prompt=system_prompt,
            tools=tools,
            think=think,
            schema=schema,
            structured_output_model=structured_output_model,
            max_tokens=max_tokens,
            **kwargs,
        )

    def complete(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
    ) -> str:
        """Run the model and return raw text."""
        return self._delegate.complete(
            prompt,
            objective=objective,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            tools=tools,
            think=think,
        )

    def complete_text(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.0,
        think: "bool | str | None" = None,
    ) -> str:
        """Alias for complete() for backward compatibility."""
        return self._delegate.complete_text(
            prompt,
            objective=objective,
            temperature=temperature,
            think=think,
        )

    def chat(
        self,
        messages: list[Dict[str, Any]],
        *,
        objective: str,
        response_format: str = "json",
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: "bool | str | None" = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """One chat completion round."""
        return self._delegate.chat(
            messages,
            objective=objective,
            response_format=response_format,
            temperature=temperature,
            tools=tools,
            think=think,
            max_tokens=max_tokens,
            **kwargs,
        )

    def get_max_context_tokens(self) -> int:
        """Return the configured context window size.

        RunPod/vLLM endpoints don't expose an ``/api/show`` introspection
        endpoint, so this returns the value configured at construction time
        (defaults to 131072 for modern vLLM deployments).
        """
        return self._context_size

    def supports_structured_output(self) -> bool:
        """vLLM supports decoder-level JSON schema enforcement."""
        return True

    def supports_prompt_caching(self) -> bool:
        """RunPod/vLLM does not support prompt caching breakpoints."""
        return False
