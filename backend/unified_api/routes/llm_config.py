"""LLM provider configuration API.

The Postgres-backed ordered **provider list** is the sole source of LLM
configuration (there is no single-provider env fallback). An operator manages the
list — each entry's provider/model/base URL and its own API key — from the UI.
Values are stored Fernet-encrypted in shared Postgres, so every team container
reads them back through ``shared.postgres`` / ``llm_service.provider_store`` — see
``llm_service/README.md``.

Endpoints:
- ``GET/POST /api/llm-config/providers`` and ``PUT/DELETE /api/llm-config/providers/{id}``,
  ``PUT /api/llm-config/providers/order`` -> manage the ordered fallback list
  (API keys are never returned — only ``api_key_configured``). Require Postgres.
- ``GET  /api/llm-config/ollama-models`` -> the live model list from the effective
  Ollama endpoint (``/api/tags``), or the curated fallback when it can't be reached.

Security: these endpoints have no app-level authentication dependency, and the
``SecurityGatewayMiddleware`` (which only content-scans the team route prefixes)
does NOT cover ``/api/llm-config``. They are intended as **operator-only**
configuration endpoints, expected to be reachable only behind the deployment's
external/network access controls (the same trust boundary as the rest of the
admin surface). API key *values* are never returned, so a read cannot exfiltrate
stored secrets. If this app is ever exposed to untrusted clients, gate these
routes with a real auth dependency.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from llm_service import clear_client_cache, provider_store, runtime_config
from llm_service import config as llm_config
from llm_service.clients import list_ollama_models
from shared.postgres import (
    StorageStatus,
    bounded_probe,
    connect_timeout,
    is_postgres_enabled,
    resolve_storage_status,
)

logger = logging.getLogger(__name__)


async def _probe_storage_status() -> StorageStatus:
    """Resolve the runtime-store status off the event loop, bounded.

    Preconditions: none.
    Postconditions: returns the shared :func:`resolve_storage_status` classification
        (``available`` / ``unconfigured`` / ``unreachable``). The blocking probe runs in
        a worker thread via the shared :func:`shared.postgres.bounded_probe`, whose budget
        (``connect_timeout + statement_timeout + 1s``) gives the inner ``SELECT 1`` real
        headroom so a slow-but-alive store isn't falsely reported unreachable, and which
        logs the cause on timeout/error rather than masking a non-connectivity bug as
        "Postgres down". NOTE: this page probes the SHARED POOL (a ``SELECT 1``), a
        different path than the GitHub panel (which reads the credential store); under
        partial Postgres degradation the two surfaces can legitimately disagree, because
        they check different stores. Never raises.
    """
    if not is_postgres_enabled():
        return "unconfigured"
    # The inner probe's own pool-acquire/connect bound is tied to connect_timeout; the
    # shared bounded_probe budget adds statement_timeout headroom on top.
    return await bounded_probe(
        lambda: resolve_storage_status(timeout_s=connect_timeout()),
        on_failure=lambda: "unreachable",
        label="LLM provider storage probe",
    )


router = APIRouter(prefix="/api/llm-config", tags=["llm-config"])

# Curated Ollama model suggestions for the /ollama-models fallback, sourced from
# llm_service.config so the UI list can't drift from the models the clients use.
# The model field also accepts free text, so this is a suggestion, not a closed set.
_OLLAMA_MODEL_SUGGESTIONS = list(llm_config.OLLAMA_MODEL_SUGGESTIONS)


def _validate_ollama_base_url_value(v: str) -> str:
    """Reject a malformed Ollama base URL; shared by every settings model.

    Preconditions: none. Postconditions: an empty value passes (means "unchanged"
        / "use default"); a non-empty value must be a well-formed http/https URL
        (scheme + host) and must NOT embed credentials (``user:pass@host``), else
        ValueError — a bad URL would be stored and break every Ollama request, and a
        credential-bearing URL would leak secrets into the store and request logs.
    """
    if not v or not v.strip():
        return v
    parsed = urlparse(v.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("ollama_base_url must be an http(s) URL, e.g. http://localhost:11434")
    if parsed.username or parsed.password:
        raise ValueError("ollama_base_url must not contain credentials (user:pass@host)")
    return v


def _is_ollama_cloud_url(url: str) -> bool:
    """Return True when ``url`` points at the Ollama Cloud endpoint.

    Preconditions: ``url`` is a string (may be empty or malformed).
    Postconditions: returns True iff the parsed hostname is exactly ``ollama.com``
        or a subdomain of it (``*.ollama.com``); returns False for an empty,
        unparseable, or non-cloud URL. Never raises. Used to gate the
        Ollama-Cloud-without-key guard so a local Ollama URL (e.g.
        ``http://localhost:11434``) never trips it.
    """
    host = (urlparse(url.strip()).hostname or "").lower() if url and url.strip() else ""
    return host == "ollama.com" or host.endswith(".ollama.com")


# ---------------------------------------------------------------------------
# RunPod-specific helper functions
# ---------------------------------------------------------------------------


def _validate_runpod_endpoint_id(endpoint_id: str) -> str:
    """Validate and return the endpoint_id, raising ValueError on bad format.

    Only alphanumeric characters (a-zA-Z0-9) are accepted. A valid endpoint ID
    is a non-empty string containing exclusively letters and digits — no spaces,
    hyphens, slashes, or other special characters that would corrupt a URL path
    segment.

    Preconditions: ``endpoint_id`` is a string.
    Postconditions: returns ``endpoint_id`` unchanged when it matches
        ``^[a-zA-Z0-9]+$``; raises ``ValueError`` with a descriptive message
        otherwise. Never makes network calls.
    """
    import re

    if not re.fullmatch(r"[a-zA-Z0-9]+", endpoint_id):
        raise ValueError(
            "endpoint_id must contain only alphanumeric characters (letters and digits)."
        )
    return endpoint_id


def _build_runpod_base_url(endpoint_id: str) -> str:
    """Construct the canonical RunPod OpenAI-compatible base URL for an endpoint.

    Preconditions: ``endpoint_id`` is a valid alphanumeric string (caller must
        validate first via ``_validate_runpod_endpoint_id``).
    Postconditions: returns exactly
        ``https://api.runpod.ai/v2/{endpoint_id}/openai/v1`` with no trailing
        slash, no extra path segments, and no query parameters.
    """
    return f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1"


#: Wall-clock ceiling (seconds) for the RunPod reachability probe. It bounds the
#: added latency of create/update requests that configure a RunPod provider — the
#: handler blocks for at most this long while the probe runs. Kept short so a slow
#: or unreachable endpoint fails fast instead of tripping client/gateway timeouts.
_RUNPOD_PROBE_TIMEOUT_SECONDS = 5.0


async def _probe_runpod_endpoint(endpoint_id: str, api_key: str) -> None:
    """Send a GET to the RunPod /models endpoint to verify it is reachable.

    Fires a single authenticated GET request to
    ``https://api.runpod.ai/v2/{endpoint_id}/openai/v1/models`` with a
    ``_RUNPOD_PROBE_TIMEOUT_SECONDS`` timeout. Used at create/update time to
    validate that the supplied credentials work before persisting the entry.

    Preconditions: ``endpoint_id`` has passed ``_validate_runpod_endpoint_id``;
        ``api_key`` is non-empty (caller already checked).
    Postconditions: returns ``None`` when the endpoint responds with a 2xx status.
        On failure raises an ``HTTPException`` whose status reflects the failure
        class rather than always 400:

        - a non-2xx response from RunPod is surfaced with the *remote* status code
          (e.g. 401 for a bad key, 404 for an unknown endpoint, 5xx for a RunPod
          server error) so the caller isn't told a valid request was malformed;
        - a connection error or timeout — the endpoint never answered — maps to
          503 (upstream unreachable).
    """
    import httpx

    url = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1/models"
    try:
        async with httpx.AsyncClient(timeout=_RUNPOD_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # RunPod answered with a 4xx/5xx — propagate its status code so an auth
        # failure reads as 401, an unknown endpoint as 404, a RunPod outage as 5xx,
        # instead of misrepresenting a remote error as a client-side 400.
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"RunPod endpoint returned {e.response.status_code}: {e}",
        ) from e
    except httpx.HTTPError as e:
        # Connection refused/reset, DNS failure, or timeout — the endpoint never
        # produced a response, so it is unreachable (503), not a bad request.
        raise HTTPException(
            status_code=503,
            detail=f"RunPod endpoint could not be reached: {e}",
        ) from e


class OllamaModelsResponse(BaseModel):
    """Response for ``GET /api/llm-config/ollama-models``.

    ``source`` lets the UI tell apart a live listing from the curated fallback so
    it can hint when the endpoint couldn't be reached.
    """

    models: list[str] = Field(..., description="Available model ids (live or fallback).")
    base_url: str = Field(..., description="Effective Ollama base URL the list was fetched from.")
    source: Literal["live", "fallback"] = Field(
        ..., description="'live' when fetched from /api/tags, 'fallback' for the curated list."
    )


@router.get("/ollama-models", response_model=OllamaModelsResponse)
async def get_ollama_models() -> OllamaModelsResponse:
    """Return the live Ollama model list for the settings dropdown.

    Queries the effective Ollama endpoint's ``/api/tags`` (via
    ``llm_service.clients.list_ollama_models``, which uses the resolved base URL
    and Ollama key — so a Cloud key saved through this page authenticates the
    request, and a local endpoint needs none). No secret is accepted in the request
    or returned in the response.

    Preconditions: none. Works regardless of Postgres (reads fall back to env).
    Postconditions: returns ``source="live"`` with the fetched model ids when the
        endpoint responds with a non-empty list; otherwise returns
        ``source="fallback"`` with the curated ``OLLAMA_MODEL_SUGGESTIONS`` so the
        dropdown is never empty. ``base_url`` is the resolved effective endpoint.
        Never raises (the underlying fetch degrades to ``[]`` on any error).
    """
    base_url = llm_config.resolve_base_url()
    live = list_ollama_models()
    if live:
        return OllamaModelsResponse(models=live, base_url=base_url, source="live")
    return OllamaModelsResponse(models=list(_OLLAMA_MODEL_SUGGESTIONS), base_url=base_url, source="fallback")


# ---------------------------------------------------------------------------
# Multi-provider fallback list — ordered providers with usage-limit state.
# ---------------------------------------------------------------------------


class LlmProviderEntryResponse(BaseModel):
    """One configured provider in the fallback list. API keys are never returned."""

    id: int
    label: str
    provider: str
    model: str
    base_url: str
    sort_order: int
    api_key_configured: bool = Field(
        False, description="True when this entry has a stored API key (the value is never returned)."
    )
    limit_exceeded: bool = Field(False, description="True while this provider is usage-limited.")
    limit_type: str = Field(
        "",
        description="Lightweight label for the limit (e.g. 'rate', 'session', 'weekly').",
    )
    reset_at: datetime | None = Field(
        None, description="When the usage limit is expected to reset (UTC); null when not limited."
    )


class LlmProviderListResponse(BaseModel):
    """Ordered provider list (most->least preferred) plus storage status for the UI."""

    providers: list[LlmProviderEntryResponse]
    storage_available: bool = Field(..., description="True only when the runtime store is configured AND reachable.")
    storage_status: StorageStatus


class LlmProviderCreate(BaseModel):
    """Request body to add a provider to the fallback list."""

    label: str = Field("", description="Human-readable name, e.g. 'Anthropic API'. Defaults to 'RunPod' for the runpod provider.")
    provider: Literal["ollama", "claude", "runpod"] = Field(..., description="Provider type.")
    model: str = Field("", description="Model id for the provider (empty = provider default).")
    base_url: str = Field("", description="Ollama base URL (empty = default); ignored for Claude and RunPod.")
    api_key: str = Field("", description="API key for the provider (never returned by GET).")
    endpoint_id: str = Field("", description="RunPod endpoint ID (alphanumeric). Required when provider='runpod'.")

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str, info: ValidationInfo) -> str:
        # ``base_url`` is an Ollama-only field — it is ignored for Claude and RunPod
        # (whose URL is derived from ``endpoint_id``). Only enforce the Ollama URL
        # shape for an actual Ollama entry so a stray value on a non-Ollama provider
        # doesn't 422. ``provider`` is declared first, so it's already in ``info.data``.
        if info.data.get("provider") != "ollama":
            return v
        return _validate_ollama_base_url_value(v)


class LlmProviderUpdate(BaseModel):
    """Request body to edit a provider; omitted/empty fields leave the stored value.

    ``api_key`` empty leaves the existing key untouched (so the UI can save other
    edits without re-entering it), mirroring ``PUT /api/llm-config``.
    """

    # No ``min_length`` here: per the contract an empty/omitted field means "leave
    # unchanged" (normalized to None in the handler), so an empty label must not 422.
    label: str | None = Field(None, description="New label; empty/omitted leaves it unchanged.")
    provider: Literal["ollama", "claude", "runpod"] | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str = Field("", description="New API key; empty leaves the stored key unchanged.")
    endpoint_id: str = Field("", description="New RunPod endpoint ID (alphanumeric). Empty leaves the stored base_url unchanged.")
    clear_api_key: bool = Field(
        False,
        description=(
            "When true, remove the stored API key (e.g. switching to a keyless local "
            "Ollama). Ignored when a non-empty api_key is also provided."
        ),
    )

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str | None, info: ValidationInfo) -> str | None:
        # ``base_url`` is Ollama-only (ignored for Claude/RunPod). Skip the URL-shape
        # check when the provider is explicitly non-Ollama; keep it when ``provider``
        # is omitted (None = "unchanged"), since the stored entry may well be Ollama.
        if info.data.get("provider") in ("claude", "runpod"):
            return v
        return v if v is None else _validate_ollama_base_url_value(v)


class LlmProviderOrderUpdate(BaseModel):
    """Request body to reorder the list: the full set of ids, most->least preferred."""

    ids: list[int] = Field(..., description="Provider ids in the new preference order.")


def _entry_to_response(entry: provider_store.ProviderEntry) -> LlmProviderEntryResponse:
    """Map a stored entry to its API shape, masking the API key.

    Postconditions: ``api_key_configured`` reflects whether a key is stored; the key
        value itself is never included. Never raises.
    """
    return LlmProviderEntryResponse(
        id=entry.id,
        label=entry.label,
        provider=entry.provider,
        model=entry.model,
        base_url=entry.base_url,
        sort_order=entry.sort_order,
        api_key_configured=bool(entry.api_key),
        limit_exceeded=entry.limit_exceeded,
        limit_type=entry.limit_type,
        reset_at=entry.reset_at,
    )


async def _provider_list_response() -> LlmProviderListResponse:
    """Assemble the current ordered provider list + freshly-probed storage status.

    Drops the provider-list cache first so the view always reflects the committed
    store even when this request lands on a different worker than the mutation.

    Postconditions: returns the list ordered most->least preferred with keys masked.
        Never raises — any read failure (probe, store read, or mapping) degrades to an
        empty list with ``storage_status="unreachable"`` rather than a 500, so a
        transient DB blip never breaks the settings UI (incl. after a successful
        mutation, where this assembles the response).
    """
    try:
        provider_store.clear_cache()
    except Exception:  # noqa: BLE001 - a cache-clear failure must never 500 a read
        logger.warning("Failed to clear provider-list cache for GET providers", exc_info=True)
    # _probe_storage_status is itself no-raise (bounded_probe with on_failure), but the
    # store read + mapping are wrapped so a DB error degrades gracefully instead of 500ing.
    storage_status = await _probe_storage_status()
    try:
        entries = provider_store.load_ordered_entries(use_cache=False)
        providers = [_entry_to_response(e) for e in entries]
    except Exception:  # noqa: BLE001 - honor the "never raises" contract; degrade gracefully
        logger.exception("Failed to read the LLM provider list")
        providers = []
        storage_status = "unreachable"
    return LlmProviderListResponse(
        providers=providers,
        storage_available=storage_status == "available",
        storage_status=storage_status,
    )


def _require_storage() -> None:
    """Raise 503 when the runtime store is not configured.

    Postconditions: returns normally only when ``POSTGRES_HOST`` is set; otherwise
        raises ``HTTPException(503)`` — the provider list is Postgres-only, and it is
        the sole source of LLM configuration (no env fallback).
    """
    if not is_postgres_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "POSTGRES_HOST is not set; the LLM provider list cannot be persisted. "
                "Set the Postgres env vars to configure LLM providers."
            ),
        )


def _guard_entry_credentials(provider: str, base_url: str, effective_api_key: str) -> None:
    """Reject a provider entry that cannot work without its OWN key (per-entry guards).

    The provider list is the sole source of LLM resolution and each entry is
    self-contained: a Claude entry needs its own key; an Ollama entry pointed at
    Ollama Cloud needs its own key; a local Ollama URL needs none. There is NO env
    fallback — an entry can never rely on ``LLM_CLAUDE_API_KEY`` / ``ANTHROPIC_API_KEY``
    / ``OLLAMA_API_KEY`` at call time, so it must not be allowed to persist keyless.

    Preconditions: ``provider`` is ``"ollama"``/``"claude"``/``"runpod"``. Postconditions: raises
        ``HTTPException(400)`` when the required key is absent; returns otherwise.
    """
    if provider == "runpod":
        if not effective_api_key:
            raise HTTPException(
                status_code=400,
                detail="Cannot configure a RunPod provider without an API key. Provide api_key.",
            )
        return
    if provider == "claude":
        if not effective_api_key:
            raise HTTPException(
                status_code=400,
                detail="Cannot configure a Claude provider without an API key. Provide api_key.",
            )
        return
    # Ollama: only the cloud endpoint requires a key.
    effective_base_url = base_url.strip() or llm_config.resolve_base_url()
    if _is_ollama_cloud_url(effective_base_url) and not effective_api_key:
        raise HTTPException(
            status_code=400,
            detail="Cannot use Ollama Cloud without an API key. Provide api_key.",
        )


def _refresh_caches_after_mutation() -> None:
    """Drop the runtime/provider/client caches so the new list takes effect now.

    Postconditions: the runtime-config, provider-list, and provider-client caches
        are cleared in this process; other containers pick the change up within the
        TTL. A cache-clear failure is logged, never raised (the write already
        committed).
    """
    try:
        runtime_config.clear_cache()
        clear_client_cache()  # also clears the provider-list cache (provider_store.clear_cache)
    except Exception:  # noqa: BLE001 - never 500 after a successful persist
        logger.exception("Failed to clear caches after a provider-list mutation")


@router.get("/providers", response_model=LlmProviderListResponse)
async def list_providers() -> LlmProviderListResponse:
    """Return the ordered provider fallback list (API keys masked)."""
    return await _provider_list_response()


@router.post("/providers", response_model=LlmProviderListResponse)
async def create_provider(body: LlmProviderCreate) -> LlmProviderListResponse:
    """Add a provider to the end of the fallback list.

    For a RunPod provider this performs a synchronous reachability probe against
    the endpoint before persisting, so the request can block for up to
    ``_RUNPOD_PROBE_TIMEOUT_SECONDS`` (currently 5 s) waiting on the network.

    Preconditions: Postgres configured; the per-entry key guards pass. Postconditions:
        the entry is persisted (api key encrypted, whitespace-trimmed) at the end of
        the list, caches are refreshed, and the full list is returned.
    """
    _require_storage()
    _guard_entry_credentials(body.provider, body.base_url, body.api_key.strip())
    if body.provider == "runpod":
        if not body.endpoint_id.strip():
            raise HTTPException(status_code=400, detail="endpoint_id is required for RunPod.")
        try:
            _validate_runpod_endpoint_id(body.endpoint_id.strip())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await _probe_runpod_endpoint(body.endpoint_id.strip(), body.api_key.strip())
        base_url = _build_runpod_base_url(body.endpoint_id.strip())
        label = body.label.strip() or "RunPod"
    else:
        if not body.label.strip():
            raise HTTPException(status_code=400, detail="label must not be empty.")
        base_url = body.base_url
        label = body.label.strip()
    try:
        provider_store.create_entry(
            label=label,
            provider=body.provider,
            model=body.model,
            base_url=base_url,
            api_key=body.api_key.strip(),
        )
    except Exception as e:  # noqa: BLE001 - surface a clear 503 instead of an opaque 500
        logger.exception("Failed to create LLM provider entry")
        raise HTTPException(status_code=503, detail="Failed to persist provider: storage error.") from e
    _refresh_caches_after_mutation()
    return await _provider_list_response()


@router.put("/providers/order", response_model=LlmProviderListResponse)
async def reorder_providers(body: LlmProviderOrderUpdate) -> LlmProviderListResponse:
    """Reassign the fallback order to match ``ids`` (most->least preferred).

    Declared before ``/providers/{entry_id}`` so the literal ``order`` path is not
    captured by the typed ``{entry_id}`` route.

    Preconditions: Postgres configured; ``ids`` lists the provider ids in the new
        order. Postconditions: each id's ``sort_order`` equals its position (one
        atomic transaction), caches refreshed, and the reordered list returned.
    """
    _require_storage()
    # ``reorder`` validates that ``ids`` is an exact permutation of the live id set
    # *inside its transaction* (under a row lock), so the check and the writes are
    # atomic and a concurrent create/delete can't make a stale set slip through —
    # a mismatch raises ReorderMismatchError, surfaced here as 400.
    try:
        provider_store.reorder(body.ids)
    except provider_store.ReorderMismatchError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 - surface a clear 503 instead of an opaque 500
        logger.exception("Failed to reorder LLM provider list")
        raise HTTPException(status_code=503, detail="Failed to reorder providers: storage error.") from e
    _refresh_caches_after_mutation()
    return await _provider_list_response()


@router.put("/providers/{entry_id}", response_model=LlmProviderListResponse)
async def update_provider(entry_id: int, body: LlmProviderUpdate) -> LlmProviderListResponse:
    """Edit one provider; omitted/empty fields keep the stored value.

    Unlike ``create_provider`` this does not probe the RunPod endpoint, so it adds
    no network latency: a new ``endpoint_id`` is only validated and used to rebuild
    the stored base URL.

    Preconditions: the entry exists; Postgres configured; the per-entry key guards
        pass for the resulting (merged) provider/base_url/key. Postconditions: the
        named fields are updated, caches refreshed, and the full list returned.
    """
    _require_storage()
    existing = provider_store.get_entry(entry_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Provider {entry_id} not found")
    # Per the contract, an empty/whitespace text field means "leave unchanged": normalize
    # it to None so update_entry (which treats None as unchanged) never clears a stored
    # value — mirrors the api_key handling and the single-provider PUT. Without this, a
    # client sending model:"" or base_url:"" would silently wipe the field.
    label = body.label.strip() if (body.label and body.label.strip()) else None
    model = body.model.strip() if (body.model and body.model.strip()) else None
    base_url = body.base_url.strip() if (body.base_url and body.base_url.strip()) else None
    # API key resolution: a non-empty api_key sets it; otherwise clear_api_key=True
    # removes it ("" for the store); otherwise leave it unchanged (None for the store).
    new_api_key = body.api_key.strip()
    if new_api_key:
        api_key_arg: str | None = new_api_key
    elif body.clear_api_key:
        api_key_arg = ""  # explicit removal
    else:
        api_key_arg = None  # unchanged
    # Merge updates over the existing entry for the guard: the effective key is the new
    # one (if given), "" when cleared, else the existing — so the guard still rejects a
    # Claude / Ollama-Cloud entry left without any usable key.
    merged_provider = body.provider or existing.provider
    merged_base_url = base_url if base_url is not None else existing.base_url
    if new_api_key:
        effective_api_key = new_api_key
    elif body.clear_api_key:
        effective_api_key = ""
    else:
        effective_api_key = existing.api_key
    _guard_entry_credentials(merged_provider, merged_base_url, effective_api_key)
    # RunPod branch: when endpoint_id is provided, validate it and reconstruct base_url.
    # This overrides any body.base_url value (which is ignored for RunPod entries) and
    # ensures update_entry receives a non-None base_url so the connection-affecting field
    # change triggers limit-state clearing per requirements 2.7 and 2.8.
    # When endpoint_id is absent/empty, base_url remains None (leave stored value unchanged)
    # per requirement 2.2.
    if merged_provider == "runpod" and body.endpoint_id.strip():
        try:
            _validate_runpod_endpoint_id(body.endpoint_id.strip())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        base_url = _build_runpod_base_url(body.endpoint_id.strip())
    try:
        updated = provider_store.update_entry(
            entry_id,
            label=label,
            provider=body.provider,
            model=model,
            base_url=base_url,
            api_key=api_key_arg,
        )
    except Exception as e:  # noqa: BLE001 - surface a clear 503 instead of an opaque 500
        logger.exception("Failed to update LLM provider entry %s", entry_id)
        raise HTTPException(status_code=503, detail="Failed to persist provider: storage error.") from e
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Provider {entry_id} not found")
    _refresh_caches_after_mutation()
    return await _provider_list_response()


@router.delete("/providers/{entry_id}", response_model=LlmProviderListResponse)
async def delete_provider(entry_id: int) -> LlmProviderListResponse:
    """Remove a provider from the fallback list.

    Preconditions: Postgres configured. Postconditions: the entry is gone (404 when
        it never existed), caches refreshed, and the remaining list returned.
    """
    _require_storage()
    try:
        deleted = provider_store.delete_entry(entry_id)
    except Exception as e:  # noqa: BLE001 - surface a clear 503 instead of an opaque 500
        logger.exception("Failed to delete LLM provider entry %s", entry_id)
        raise HTTPException(status_code=503, detail="Failed to delete provider: storage error.") from e
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Provider {entry_id} not found")
    _refresh_caches_after_mutation()
    return await _provider_list_response()
