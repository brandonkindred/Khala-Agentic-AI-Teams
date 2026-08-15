"""
RunPod LLM client for the central LLM service.

``RunPodLLMClient`` is a thin identity subclass of ``OllamaLLMClient``. It
exists for two reasons:

1. **Type identity** — ``isinstance(client, RunPodLLMClient)`` lets the
   factory and test code distinguish a RunPod-backed client from a plain
   Ollama-backed one without inspecting base_url strings.

2. **Mandatory-key enforcement** — RunPod serverless endpoints always require
   a Bearer token. Making ``api_key`` a required keyword argument (no default)
   means a ``RunPodLLMClient`` can never be constructed without one; the
   constraint is enforced at the Python language level rather than relying on
   runtime guards elsewhere.

All wire behaviour — OpenAI-compatible POST /v1/chat/completions, streaming,
429 backoff/retry, structured-output parsing, telemetry — is inherited from
``OllamaLLMClient`` unchanged.
"""

from __future__ import annotations

from typing import Callable, Optional

from .ollama import OllamaLLMClient


class RunPodLLMClient(OllamaLLMClient):
    """RunPod serverless LLM client.

    A thin identity subclass of :class:`OllamaLLMClient`. RunPod exposes
    vLLM-backed endpoints through an OpenAI-compatible API at
    ``https://api.runpod.ai/v2/{endpoint_id}/openai/v1``, so no wire-protocol
    changes are needed — all behaviour is inherited.

    The only difference from the base class is that ``api_key`` is a required
    keyword argument here (no default value). RunPod endpoints always require
    an ``Authorization: Bearer <api_key>`` header, and this signature makes the
    requirement explicit and impossible to forget at construction time.
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
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            timeout=timeout,
            on_reasoning=on_reasoning,
            rate_limit_max_retries=rate_limit_max_retries,
            api_key=api_key,
        )
