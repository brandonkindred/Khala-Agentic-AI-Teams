"""Runtime LLM configuration read from the shared encrypted secret store.

The settings UI (``PUT /api/llm-config`` in the unified API) writes provider /
model / API keys into the ``encrypted_integration_credentials`` table under the
``llm_config`` service. Every team container reads them back through this module
via ``shared_postgres.secrets`` — no dependency on ``unified_api``.

Reads are cached for a short TTL so a UI change propagates to all containers
within the TTL without any cross-container signalling, while keeping the common
resolve path off the database on every single LLM call. ``clear_cache()`` drops
the cache immediately (the PUT endpoint calls it locally; other containers pick
the change up at the next TTL expiry).

When Postgres is disabled (``POSTGRES_HOST`` unset), every getter returns ``""``
so env-var configuration remains the sole source — non-Postgres dev and tests
are unaffected.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

SERVICE = "llm_config"

# Credential keys stored under the ``llm_config`` service.
KEY_PROVIDER = "provider"
# Per-provider model selections live under separate keys so a model chosen for one
# provider never collides with (or leaks into) the other; a provider switch in the
# UI is lossless (each provider remembers its own model).
KEY_OLLAMA_MODEL = "ollama_model"
KEY_CLAUDE_MODEL = "claude_model"
KEY_CLAUDE_API_KEY = "claude_api_key"
KEY_OLLAMA_BASE_URL = "ollama_base_url"
KEY_OLLAMA_API_KEY = "ollama_api_key"

ALL_KEYS = (
    KEY_PROVIDER,
    KEY_OLLAMA_MODEL,
    KEY_CLAUDE_MODEL,
    KEY_CLAUDE_API_KEY,
    KEY_OLLAMA_BASE_URL,
    KEY_OLLAMA_API_KEY,
)

# Authoritative provider -> model-key mapping for the write side (the
# ``PUT /api/llm-config`` handler picks the storage key by provider). The values
# are the same constants the per-provider resolvers read back
# (``resolve_model`` -> ``KEY_OLLAMA_MODEL``, ``resolve_claude_model`` ->
# ``KEY_CLAUDE_MODEL``), so the model a provider is saved under is always the one
# its resolver reads. Adding a provider means one entry here plus its resolver.
PROVIDER_MODEL_KEYS = {
    "ollama": KEY_OLLAMA_MODEL,
    "claude": KEY_CLAUDE_MODEL,
}

ENV_RUNTIME_TTL = "LLM_RUNTIME_CONFIG_TTL_S"
_DEFAULT_TTL_S = 30.0

_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_ts: float = 0.0


def _ttl_seconds() -> float:
    """Return the runtime-config cache TTL in seconds (env override, defensive).

    Postconditions: returns a non-negative float; a missing or unparseable
        ``LLM_RUNTIME_CONFIG_TTL_S`` yields ``_DEFAULT_TTL_S``; a negative value
        is floored to ``0.0`` (read-through on every call). Never raises.
    """
    raw = os.environ.get(ENV_RUNTIME_TTL)
    if not raw:
        return _DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_TTL_S


def _postgres_enabled() -> bool:
    """True when Postgres is configured. Lazy import so non-PG envs stay clean."""
    try:
        from shared_postgres import is_postgres_enabled

        return is_postgres_enabled()
    except Exception:  # noqa: BLE001 - shared_postgres optional at import time
        return False


def _load_all() -> dict[str, str]:
    """Read every runtime key from the secret store in ONE query. ``{}`` on failure.

    Postconditions: returns a dict mapping each key in ``ALL_KEYS`` present in the
        store to its value (absent/empty values omitted), fetched with a single
        batched ``get_secrets`` round-trip. Any error yields ``{}`` so the resolver
        falls back to env vars. Never raises.
    """
    if not _postgres_enabled():
        return {}
    try:
        from shared_postgres.secrets import get_secrets
    except Exception:  # noqa: BLE001 - defensive
        return {}
    try:
        values = get_secrets(SERVICE, ALL_KEYS)
    except Exception as e:  # noqa: BLE001 - read must never crash a caller
        logger.debug("runtime_config batch read failed: %s", e)
        return {}
    return {key: val for key, val in values.items() if val}


def _refresh_locked() -> None:
    """Reload the cache from the store. Caller must hold ``_lock``."""
    global _cache, _cache_ts
    _cache = _load_all()
    _cache_ts = time.monotonic()


def get_runtime(key: str) -> str:
    """Return the runtime value for ``key`` (cached), or ``""`` when unset.

    Preconditions: ``key`` is one of :data:`ALL_KEYS`.
    Postconditions: returns the stored value, or ``""`` when absent, Postgres is
        disabled, or the store read failed. All keys are loaded together in a
        single batched query at most once per TTL window. Never raises.
    """
    assert key in ALL_KEYS, f"unknown runtime key: {key!r}"
    ttl = _ttl_seconds()
    with _lock:
        if _cache_ts == 0.0 or (time.monotonic() - _cache_ts) >= ttl:
            _refresh_locked()
        return _cache.get(key, "")


def clear_cache() -> None:
    """Drop the cached runtime config so the next read reloads from the store.

    Postconditions: the next :func:`get_runtime` reloads from Postgres. Safe to
        call when nothing was cached.
    """
    global _cache_ts
    with _lock:
        _cache.clear()
        _cache_ts = 0.0


def snapshot() -> dict[str, str]:
    """Return a fresh, uncached copy of all runtime values (for the GET endpoint).

    Postconditions: returns a dict over :data:`ALL_KEYS` read directly from the
        store (bypassing the TTL cache), so a settings page always shows the
        committed state. ``{}`` when Postgres is disabled.
    """
    return _load_all()
