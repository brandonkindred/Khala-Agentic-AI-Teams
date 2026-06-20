"""LLM provider configuration API.

Lets an operator choose the LLM provider (Ollama or Claude), the model, and the
API keys from the UI instead of (or in addition to) environment variables. Values
are stored Fernet-encrypted in the shared ``encrypted_integration_credentials``
table under the ``llm_config`` service, so every team container reads them back
through ``shared_postgres.secrets`` / ``llm_service.runtime_config`` — see
``llm_service/README.md``.

Endpoints:
- ``GET  /api/llm-config`` -> effective provider/model/base URL, ``*_configured``
  booleans (keys are never returned), and the curated option lists for the UI.
- ``PUT  /api/llm-config`` -> validate and persist; empty fields leave the
  existing stored value untouched. Requires Postgres.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from llm_service import clear_client_cache, runtime_config
from llm_service import config as llm_config
from shared_postgres import is_postgres_enabled, set_secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm-config", tags=["llm-config"])

# Curated options surfaced to the settings UI. The model fields also accept free
# text, so these are suggestions, not a closed set.
_PROVIDER_OPTIONS = ["ollama", "claude"]
# Claude suggestions come from llm_config.CLAUDE_MODEL_OPTIONS (derived from the
# context-window table) so this list can't drift from the model the client uses.
_OLLAMA_MODEL_SUGGESTIONS = [
    "deepseek-v4-pro:cloud",
    "qwen3-coder:480b-cloud",
    "llama3.1",
    "llama3.2",
]


class LlmConfigUpdate(BaseModel):
    """Request body for ``PUT /api/llm-config``.

    Empty string fields leave the existing stored value untouched (so the UI can
    save provider/model changes without re-entering API keys).
    """

    provider: Literal["ollama", "claude"] = Field(..., description="Active LLM provider.")
    model: str = Field("", description="Model id for the active provider (empty = unchanged).")
    ollama_base_url: str = Field(
        "", description="Ollama base URL — local (http://host:11434) or cloud (https://ollama.com)."
    )
    claude_api_key: str = Field("", description="Anthropic API key (never returned by GET).")
    ollama_api_key: str = Field("", description="Ollama Cloud API key (never returned by GET).")


class LlmConfigResponse(BaseModel):
    """Response for ``GET``/``PUT /api/llm-config`` — never includes API keys."""

    provider: str
    model: str
    ollama_model: str = Field("", description="Effective Ollama model (lets the UI restore it on a provider switch).")
    claude_model: str = Field("", description="Effective Claude model (lets the UI restore it on a provider switch).")
    ollama_base_url: str
    claude_api_key_configured: bool = Field(False, description="True when a Claude key is set (runtime or env).")
    ollama_api_key_configured: bool = Field(False, description="True when an Ollama Cloud key is set (runtime or env).")
    storage_available: bool = Field(
        ..., description="False when POSTGRES_HOST is unset; PUT returns 503 and config is env-only."
    )
    provider_options: list[str]
    claude_model_options: list[str]
    ollama_model_suggestions: list[str]


def _build_response() -> LlmConfigResponse:
    """Assemble the current effective config for the UI (no secrets).

    Preconditions: none.
    Postconditions: returns the effective provider, the active provider's resolved
        model (via the shared ``resolve_model_for_provider`` chokepoint, so the UI
        never disagrees with the model agents actually use), each provider's
        effective model (so the UI can restore the inactive one on a provider
        switch), the Ollama base URL, and ``*_configured`` booleans — API keys are
        never included. Never raises.
    """
    return LlmConfigResponse(
        provider=llm_config.resolve_provider(),
        model=llm_config.resolve_model_for_provider(None),
        ollama_model=llm_config.resolve_model(None),
        claude_model=llm_config.resolve_claude_model(None),
        ollama_base_url=llm_config.resolve_base_url(),
        claude_api_key_configured=bool(llm_config.resolve_claude_api_key()),
        ollama_api_key_configured=bool(llm_config.resolve_ollama_api_key()),
        storage_available=is_postgres_enabled(),
        provider_options=list(_PROVIDER_OPTIONS),
        claude_model_options=list(llm_config.CLAUDE_MODEL_OPTIONS),
        ollama_model_suggestions=list(_OLLAMA_MODEL_SUGGESTIONS),
    )


@router.get("", response_model=LlmConfigResponse)
async def get_llm_config() -> LlmConfigResponse:
    """Return the effective LLM provider configuration (API keys masked).

    Preconditions: none.
    Postconditions: returns the current effective config; API keys are reported
        only as ``*_configured`` booleans (never the key values). The runtime-config
        TTL cache is dropped first so the settings page always reflects the
        committed store, even when this GET lands on a different worker than the
        PUT that wrote it (a stale per-worker cache would otherwise show old
        provider/model/key-configured flags). Reads still flow through the shared
        ``resolve_*`` chokepoint — clearing the cache forces a fresh read without
        bypassing the env-fallback/heuristic logic a raw snapshot would skip.
    """
    runtime_config.clear_cache()
    return _build_response()


@router.put("", response_model=LlmConfigResponse)
async def update_llm_config(body: LlmConfigUpdate) -> LlmConfigResponse:
    """Persist the LLM provider configuration and refresh client caches.

    Preconditions: Postgres is configured (``POSTGRES_HOST`` set) — otherwise
        returns 503, since the runtime store is the only cross-container channel.
    Postconditions: provider (and any non-empty model/base URL/key) are stored
        encrypted; the runtime-config and provider-client caches are cleared in
        this process so subsequent calls use the new config.
    """
    if not is_postgres_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "POSTGRES_HOST is not set; LLM provider config cannot be persisted. "
                "Set Postgres env vars, or configure the provider via environment variables."
            ),
        )

    # Refuse to switch the global provider to Claude unless a key will actually be
    # available (in this request, or already stored/in env). Otherwise the factory
    # builds a ClaudeLLMClient with an empty key and every later call fails with
    # LLMPermanentError until someone notices and fixes the setting.
    if body.provider == "claude" and not body.claude_api_key.strip() and not llm_config.resolve_claude_api_key():
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot switch the provider to Claude without an API key. Provide "
                "claude_api_key, or set LLM_CLAUDE_API_KEY / ANTHROPIC_API_KEY first."
            ),
        )

    # Collect every changed key and persist them in ONE transaction (set_secrets),
    # so a transient failure mid-write can never commit a half-switched config —
    # e.g. provider=claude stored while the API key write fails, leaving every
    # later LLM call broken until someone repairs the setting.
    updates: dict[str, str] = {runtime_config.KEY_PROVIDER: body.provider}
    # Empty fields are intentionally skipped so a provider/model change does not
    # wipe a previously-stored API key the operator didn't re-enter.
    if body.model.strip():
        # Store the model under the active provider's key (single source: the shared
        # PROVIDER_MODEL_KEYS map the resolvers read back from) so the two providers'
        # selections never collide and a provider switch stays lossless.
        updates[runtime_config.PROVIDER_MODEL_KEYS[body.provider]] = body.model.strip()
    if body.ollama_base_url.strip():
        updates[runtime_config.KEY_OLLAMA_BASE_URL] = body.ollama_base_url.strip()
    if body.claude_api_key.strip():
        updates[runtime_config.KEY_CLAUDE_API_KEY] = body.claude_api_key.strip()
    if body.ollama_api_key.strip():
        updates[runtime_config.KEY_OLLAMA_API_KEY] = body.ollama_api_key.strip()
    set_secrets(runtime_config.SERVICE, updates)

    # Refresh local caches immediately; other containers pick up the change within
    # the runtime-config TTL.
    runtime_config.clear_cache()
    clear_client_cache()
    logger.info("LLM provider config updated: provider=%s", body.provider)
    return _build_response()
