"""Async native-Strands LLM client for the Agent Provisioning Team.

The provisioning pipeline supports the same provider names as the rest of
Khala: ``ollama`` and ``claude`` (``anthropic`` is accepted as an alias for
the latter). Each completion uses the provider implementation from
``strands.models`` directly and consumes its asynchronous event stream.

When no model is configured, :meth:`LLMClient.complete` preserves the
deterministic, clearly labelled fallback used by the provisioning phases.
The fallback path logs a single warning per process so an unconfigured
deployment does not silently present generated-looking output to users.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from strands.models.model import Model
from strands.types.content import Message

logger = logging.getLogger(__name__)

# Characters that have no business inside an interpolated prompt variable.
# We allow letters, digits, basic punctuation and whitespace; everything else
# is removed. This is a defense-in-depth measure against prompt injection
# through manifest fields.
_PROMPT_VAR_DISALLOWED = re.compile(r"[^A-Za-z0-9 _\-./:@,()\[\]{}+=#'\"\n\t]")
_PROMPT_VAR_MAX_LEN = 100000
_SUPPORTED_PROVIDERS = frozenset({"ollama", "claude", "anthropic", "dummy"})


def sanitize_prompt_var(value: object, *, max_len: int = _PROMPT_VAR_MAX_LEN) -> str:
    """Make a manifest-supplied value safe to interpolate into an LLM prompt.

    - Coerces to str
    - Strips disallowed characters
    - Caps length at ``max_len`` (default 100k chars) to prevent a prompt-bomb
      / context-blowing input while still allowing large legitimate prompts;
      a truncated value carries a trailing ``"…[truncated]"`` marker, so its
      final length is ``max_len + len("…[truncated]")``, not exactly ``max_len``
    """
    text = "" if value is None else str(value)
    text = _PROMPT_VAR_DISALLOWED.sub("", text)
    if len(text) > max_len:
        text = text[:max_len] + "…[truncated]"
    return text


@dataclass
class LLMRequest:
    """A single LLM completion request."""

    system: str
    user: str
    temperature: float = 0.2
    max_tokens: int = 1024


class LLMClient:
    """Thin asynchronous adapter over native Strands model providers.

    A fresh native model is created for each request. Besides matching the
    lifecycle expected by the provider SDKs, this keeps per-request generation
    settings isolated when callers use one ``LLMClient`` concurrently.
    """

    _warned_fallback = False

    def __init__(
        self,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.provider = (
            provider if provider is not None else os.getenv("LLM_PROVIDER", "ollama")
        ).strip().lower()
        self.base_url = (
            base_url if base_url is not None else os.getenv("LLM_BASE_URL", "")
        ).strip()
        self.model = (model if model is not None else os.getenv("LLM_MODEL", "")).strip()
        self.api_key = self._resolve_api_key(api_key)

        if self.provider not in _SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
            raise ValueError(
                f"Unsupported LLM provider {self.provider!r}; supported providers: {supported}"
            )

    @property
    def is_configured(self) -> bool:
        """Whether this client has a live provider and a non-empty model id."""
        return self.provider != "dummy" and bool(self.model)

    async def complete(self, request: LLMRequest) -> str:
        """Run one completion asynchronously and return its text content.

        Native Strands providers expose an async event stream. Only text deltas
        are accumulated; lifecycle, reasoning, tool-use, and metadata events do
        not belong in this single-shot text API.
        """
        if not self.is_configured:
            if not LLMClient._warned_fallback:
                logger.warning(
                    "LLMClient: no LLM model configured — using deterministic fallback. "
                    "Set LLM_MODEL and provider credentials to enable native Strands completions."
                )
                LLMClient._warned_fallback = True
            return self._fallback(request)

        native_model = self._create_model(request)
        messages: list[Message] = [{"role": "user", "content": [{"text": request.user}]}]
        chunks: list[str] = []

        async for event in native_model.stream(messages, system_prompt=request.system):
            content_delta = event.get("contentBlockDelta")
            if not isinstance(content_delta, dict):
                continue
            delta = content_delta.get("delta")
            if not isinstance(delta, dict):
                continue
            text = delta.get("text")
            if isinstance(text, str):
                chunks.append(text)

        return "".join(chunks)

    def _create_model(self, request: LLMRequest) -> Model:
        """Construct the configured native Strands provider for ``request``."""
        if self.provider == "ollama":
            from strands.models.ollama import OllamaModel

            client_args = (
                {"headers": {"Authorization": f"Bearer {self.api_key}"}}
                if self.api_key
                else None
            )
            return OllamaModel(
                host=self.base_url or None,
                ollama_client_args=client_args,
                model_id=self.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )

        if self.provider in {"claude", "anthropic"}:
            from strands.models.anthropic import AnthropicModel

            client_args = {}
            if self.api_key:
                client_args["api_key"] = self.api_key
            if self.base_url:
                client_args["base_url"] = self.base_url
            return AnthropicModel(
                client_args=client_args or None,
                model_id=self.model,
                max_tokens=request.max_tokens,
                params={"temperature": request.temperature},
            )

        # ``dummy`` is deliberately never configured, so complete() returns
        # before reaching this branch. Keep the guard explicit for subclasses.
        raise RuntimeError(f"Provider {self.provider!r} cannot create a live Strands model")

    def _resolve_api_key(self, explicit_api_key: Optional[str]) -> str:
        if explicit_api_key is not None:
            return explicit_api_key.strip()
        if self.provider in {"claude", "anthropic"}:
            return (
                os.getenv("LLM_CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""
            ).strip()
        if self.provider == "ollama":
            return (os.getenv("LLM_OLLAMA_API_KEY") or os.getenv("OLLAMA_API_KEY") or "").strip()
        return ""

    @staticmethod
    def _fallback(request: LLMRequest) -> str:
        return f"[llm-fallback] {request.user.strip()}"
