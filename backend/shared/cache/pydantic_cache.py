"""Generic Pydantic-model cache policy shared by every team on ``shared.cache``.

Several agent teams need the same cache plumbing around a byte-identical-input
call whose result is a Pydantic model: a build-id-suffixed namespace, an
env-var-driven capacity floored at 0 (0 disables the cache), a whole-input
plus resolved-model SHA-256 cache key, a get/validate/corrupt-entry-delete
lookup, a set-on-genuine-outcome write, and a fail-open clear for tests/ops.
Current consumers: ``software_engineering_team``'s ``qa_agent``,
``security_agent``, every ``devops_team`` single-shot agent (via
``_agent_template.py`` and ``devsecops_review_agent``'s own call site), and
``branding_team``'s ``PhaseOutputCache`` (per-pipeline-phase output
memoization). Before this module, each of these hand-rolled its own copy of
this policy — correct, but any future fix (corrupt-entry handling, capacity
semantics, fail-open logging) had to land in every copy and could silently
drift.

This module is now the one place that policy lives. Callers supply only
their own namespace stem, env var name, capacity default, output model, and
a short label for log messages. They still import
``shared.cache.get_shared_cache`` and resolve the cache object themselves at
each call site (rather than this module resolving it internally) — this
keeps ``<caller_module>.get_shared_cache`` the seam tests monkeypatch to
simulate a broken backend, unaffected by which caller is migrated onto this
module.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable, Optional, Protocol, TypeVar

from pydantic import BaseModel

from shared.env_config import env_int

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)


class _CacheBackend(Protocol):
    """Structural shape this module needs from a ``shared.cache.SharedCache``.

    A local ``Protocol`` (rather than importing ``SharedCache`` directly)
    keeps this module's only coupling to the concrete cache implementation
    a duck-typed one, while still letting mypy/pyright validate call sites
    -- callers pass an already-resolved ``shared.cache.SharedCache``, which
    satisfies this structurally with no explicit subclassing. Mirrors
    ``shared.cache.SharedCache``'s own contract (see
    ``shared/cache/interface.py``); this module only reads/writes opaque
    bytes and never inspects ``clear``'s return value.
    """

    def get(self, key: str) -> Optional[bytes]:
        """Return the cached payload for ``key``, or ``None`` on miss.

        Preconditions:
            - ``key`` is an opaque, non-empty string.
        Postconditions:
            - Returns the exact bytes previously ``set``, or ``None``. Never
              raises for a backend outage (returns ``None`` instead).
        """
        ...

    def set(self, key: str, value: bytes, *, max_entries: int) -> None:
        """Store ``value`` under ``key``, evicting oldest entries past capacity.

        Preconditions:
            - ``max_entries`` >= 0. ``0`` means "do not store" (no-op).
        Postconditions:
            - On success the next ``get(key)`` returns ``value`` (until
              eviction or TTL). Backend failures are swallowed (fail-open).
        """
        ...

    def delete(self, key: str) -> None:
        """Drop a single key (and any associated single-flight markers).

        Preconditions:
            - ``key`` is an opaque, non-empty string.
        Postconditions:
            - Subsequent ``get(key)`` misses until a new ``set``. Backend
              failures are swallowed (fail-open).
        """
        ...

    def clear(self) -> Optional[int]:
        """Drop every entry in this namespace.

        Preconditions:
            - None.
        Postconditions:
            - Returns the number of entries removed on success, or ``None``
              when a backend failure aborts the clear (Redis fail-open).
        """
        ...


def cache_namespace_for(stem: str) -> str:
    """Build-id-suffixed shared-cache namespace for a cache stem.

    Preconditions:
        - ``stem`` is a non-empty namespace stem, e.g. ``"qa:review:v1"``.
    Postconditions:
        - Returns ``stem`` with the current build id appended (see
          ``shared.cache.with_cache_build_id``), so a deploy becomes a
          disjoint keyspace instead of requiring manual invalidation.
    """
    from shared.cache import with_cache_build_id  # noqa: PLC0415

    return with_cache_build_id(stem)


def cache_capacity_for(env_var: str, default: int) -> int:
    """Resolve a cache's capacity from its environment variable.

    Preconditions:
        - ``env_var`` is the environment variable name that controls this
          cache's capacity. ``default`` is the capacity to use when
          ``env_var`` is unset or unparseable.
    Postconditions:
        - Returns ``env_var`` parsed as an int, clamped to a floor of 0: an
          unset or unparseable value falls back to ``default``, a negative
          value clamps to 0. An explicit or clamped-to 0 disables the cache.
    """
    return env_int(env_var, default, 0)


def build_model_cache_key(input_data: BaseModel, model_fp: str) -> str:
    """Hash of the whole input model plus the resolved model identity.

    Keys the entire input model so any input-field change naturally busts
    the key with no explicit invalidation logic.

    Preconditions:
        - ``input_data`` is a Pydantic model instance whose own fields never
          include a top-level ``__model__`` key.
        - ``model_fp`` is a stable identifier for the resolved model (e.g.
          from ``llm_service.strands_model.model_fingerprint``).
    Postconditions:
        - Returns a hex digest that changes whenever any input field or the
          resolved model changes, and is stable (``sort_keys``) across calls
          in a process, so a byte-identical resubmission is recognized.
    """
    payload = input_data.model_dump(mode="json")
    payload["__model__"] = model_fp
    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def clear_cache_namespace(label: str, resolve_cache: Callable[[], _CacheBackend]) -> None:
    """Fail-open clear of a shared-cache namespace.

    Preconditions:
        - ``label`` is a short prefix for log messages (e.g. ``"QA"``,
          ``"Security"``, ``"branding-phase"``). ``resolve_cache`` returns a
          ``shared.cache.SharedCache`` when called (or raises).
    Postconditions:
        - The namespace is empty (best-effort across Redis) when this
          returns. Any exception -- resolving the cache or clearing it -- is
          caught and logged rather than propagated, so a broken backend
          never breaks a caller (e.g. a test-teardown fixture) forcing a
          cold run.
    """
    try:
        resolve_cache().clear()
    except Exception:
        logger.warning("%s: cache clear failed", label, exc_info=True)


def get_cached_model(label: str, cache: _CacheBackend, cache_key: str, output_model: type[TModel]) -> Optional[TModel]:
    """Look up ``cache_key`` in ``cache``, validating against ``output_model``.

    Preconditions:
        - ``label`` is a short prefix for log messages. ``cache`` is an
          already-resolved ``shared.cache.SharedCache``. ``output_model`` is
          the Pydantic model the cached payload was serialized from.
    Postconditions:
        - Returns the validated cached result on a clean hit. Returns
          ``None`` on a miss, a cache backend error (get or delete), or a
          corrupt entry -- which is deleted so it never masks the same key
          on a future call. Never raises.
    """
    try:
        raw = cache.get(cache_key)
    except Exception:
        logger.warning("%s: cache get failed; treating as miss", label, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return output_model.model_validate_json(raw)
    except Exception:
        logger.warning(
            "%s: corrupt cache entry for %s; treating as miss",
            label,
            cache_key,
            exc_info=True,
        )
        try:
            cache.delete(cache_key)
        except Exception:
            logger.warning("%s: cache delete failed after corrupt entry", label, exc_info=True)
        return None


def set_cached_model(label: str, cache: _CacheBackend, cache_key: str, result: BaseModel, *, capacity: int) -> None:
    """Write a genuine result back to the cache. Fail-open.

    Preconditions:
        - ``label`` is a short prefix for log messages. ``cache`` is an
          already-resolved ``shared.cache.SharedCache``. ``capacity`` is the
          caller's already-resolved (and already ``> 0``) cache capacity.
    Postconditions:
        - The entry is written best-effort. Any backend error is caught and
          logged, never propagated -- a broken cache backend never blocks a
          caller from returning its result.
    """
    payload = result.model_dump_json().encode("utf-8")
    try:
        cache.set(cache_key, payload, max_entries=capacity)
    except Exception:
        logger.warning("%s: cache set failed; continuing without cache write", label, exc_info=True)
    else:
        logger.info("%s: cached result under key=%s (bytes=%d)", label, cache_key, len(payload))


__all__ = [
    "cache_namespace_for",
    "cache_capacity_for",
    "build_model_cache_key",
    "clear_cache_namespace",
    "get_cached_model",
    "set_cached_model",
]
