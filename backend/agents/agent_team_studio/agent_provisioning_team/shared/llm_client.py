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


@dataclass(frozen=True)
class _ProviderConfig:
    """Resolved connection settings for one native Strands completion."""

    provider: str
    base_url: str
    model: str
    api_key: str


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
        has_explicit_config = any(
            value is not None for value in (provider, base_url, model, api_key)
        )
        self._explicit_config = (
            _ProviderConfig(
                provider=self._normalize_provider(provider or "ollama"),
                base_url=(base_url or "").strip(),
                model=(model or "").strip(),
                api_key=(api_key or "").strip(),
            )
            if has_explicit_config
            else None
        )

    @property
    def is_configured(self) -> bool:
        """Whether this client has a live provider and a non-empty model id."""
        config = self._resolve_config()
        return config.provider != "dummy" and bool(config.model)

    async def complete(self, request: LLMRequest) -> str:
        """Run one completion asynchronously and return its text content.

        Native Strands providers expose an async event stream. Only text deltas
        are accumulated; lifecycle, reasoning, tool-use, and metadata events do
        not belong in this single-shot text API.
        """
        config = self._resolve_config()
        if config.provider == "dummy" or not config.model:
            if not LLMClient._warned_fallback:
                logger.warning(
                    "LLMClient: no LLM model configured — using deterministic fallback. "
                    "Set LLM_MODEL and provider credentials to enable native Strands completions."
                )
                LLMClient._warned_fallback = True
            return self._fallback(request)

        native_model = self._create_model(request, config)
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

    def _create_model(
        self,
        request: LLMRequest,
        config: Optional[_ProviderConfig] = None,
    ) -> Model:
        """Construct the configured native Strands provider for ``request``."""
        resolved = config or self._resolve_config()
        if resolved.provider == "ollama":
            from strands.models.ollama import OllamaModel

            client_args = (
                {"headers": {"Authorization": f"Bearer {resolved.api_key}"}}
                if resolved.api_key
                else None
            )
            return OllamaModel(
                host=resolved.base_url or None,
                ollama_client_args=client_args,
                model_id=resolved.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )

        if resolved.provider == "claude":
            from strands.models.anthropic import AnthropicModel

            client_args = {}
            if resolved.api_key:
                client_args["api_key"] = resolved.api_key
            return AnthropicModel(
                client_args=client_args or None,
                model_id=resolved.model,
                max_tokens=request.max_tokens,
                params={"temperature": request.temperature},
            )

        # ``dummy`` is deliberately never configured, so complete() returns
        # before reaching this branch. Keep the guard explicit for subclasses.
        raise RuntimeError(
            f"Provider {resolved.provider!r} cannot create a live Strands model"
        )

    def _resolve_config(self) -> _ProviderConfig:
        """Resolve the active provider entry for the current call.

        Explicit constructor arguments remain available for tests and isolated
        callers. The default client reloads the cached provider list on every
        call so settings changes and usage-limit selection are honored without a
        process restart. Secret values never fall back to the environment.
        """
        if self._explicit_config is not None:
            return self._explicit_config

        from llm_service import config as llm_config
        from llm_service import provider_store

        if llm_config.resolve_provider() == "dummy":
            return _ProviderConfig("dummy", "", "", "")

        entry = provider_store.select_active_entry(provider_store.load_ordered_entries())
        if entry is None:
            return _ProviderConfig("dummy", "", "", "")

        provider = self._normalize_provider(entry.provider)
        model = entry.model.strip() or llm_config.resolve_model_for_provider(
            None, provider=provider
        )
        base_url = ""
        if provider == "ollama":
            base_url = entry.base_url.strip() or llm_config.resolve_base_url()
        return _ProviderConfig(provider, base_url, model, entry.api_key.strip())

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = provider.strip().lower()
        if normalized == "anthropic":
            normalized = "claude"
        if normalized not in _SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
            raise ValueError(
                f"Unsupported LLM provider {normalized!r}; supported providers: {supported}"
            )
        return normalized

    @staticmethod
    def _fallback(request: LLMRequest) -> str:
        return f"[llm-fallback] {request.user.strip()}"
