"""Generic whole-input review-result cache shared by single-shot review agents.

``qa_agent`` and ``security_agent`` each need the same cache plumbing around
a byte-identical-input LLM review call: a build-id-suffixed namespace, an
env-var-driven capacity floored at 0 (0 disables the cache), a whole-input
plus resolved-model SHA-256 cache key, a get/validate/corrupt-entry-delete
lookup, a set-on-genuine-outcome write, and a fail-open clear for tests/ops.
Before this module, each agent hand-rolled its own copy of this policy —
correct, but any future fix (corrupt-entry handling, capacity semantics,
fail-open logging) had to land in two places and could silently drift.

This module is now the one place that policy lives. ``qa_agent.agent`` and
``security_agent.agent`` each supply only their own namespace stem, env var
name, capacity default, output model, and a short label for log messages.
They still import ``shared.cache.get_shared_cache`` and resolve the cache
object themselves at each call site (rather than this module resolving it
internally) — this keeps ``<agent_module>.get_shared_cache`` the seam tests
monkeypatch to simulate a broken backend, unchanged by this refactor.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable, Optional, TypeVar

from pydantic import BaseModel

from shared.env_config import env_int

logger = logging.getLogger(__name__)

TOutput = TypeVar("TOutput", bound=BaseModel)


def cache_namespace_for(stem: str) -> str:
    """Build-id-suffixed shared-cache namespace for a review-result cache stem.

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
    """Resolve a review cache's capacity from its environment variable.

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


def build_review_cache_key(input_data: BaseModel, model_fp: str) -> str:
    """Hash of the whole input model plus the resolved review model.

    Keys the entire input model so any reviewed-file byte change naturally
    busts the key with no explicit invalidation logic.

    Preconditions:
        - ``input_data`` is a Pydantic model instance whose own fields never
          include a top-level ``__model__`` key.
        - ``model_fp`` is a stable identifier for the resolved review model
          (e.g. from ``llm_service.strands_model.model_fingerprint``).
    Postconditions:
        - Returns a hex digest that changes whenever any input field or the
          resolved model changes, and is stable (``sort_keys``) across calls
          in a process, so a byte-identical resubmission is recognized.
    """
    payload = input_data.model_dump(mode="json")
    payload["__model__"] = model_fp
    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def clear_review_cache_namespace(label: str, resolve_cache: Callable[[], object]) -> None:
    """Fail-open clear of a review-result cache namespace.

    Preconditions:
        - ``label`` is a short prefix for log messages (e.g. ``"QA"``,
          ``"Security"``). ``resolve_cache`` returns a
          ``shared.cache.SharedCache`` when called (or raises).
    Postconditions:
        - The namespace is empty (best-effort across Redis) when this
          returns. Any exception -- resolving the cache or clearing it -- is
          caught and logged rather than propagated, so a broken backend
          never breaks a caller (e.g. a test-teardown fixture) forcing a
          cold review.
    """
    try:
        resolve_cache().clear()
    except Exception:
        logger.warning("%s: review cache clear failed", label, exc_info=True)


def get_cached_review_result(
    label: str, cache: object, cache_key: str, output_model: type[TOutput]
) -> Optional[TOutput]:
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
        logger.warning("%s: review cache get failed; treating as miss", label, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return output_model.model_validate_json(raw)
    except Exception:
        logger.warning(
            "%s: corrupt review cache entry for %s; treating as miss",
            label,
            cache_key,
            exc_info=True,
        )
        try:
            cache.delete(cache_key)
        except Exception:
            logger.warning(
                "%s: review cache delete failed after corrupt entry", label, exc_info=True
            )
        return None


def set_cached_review_result(
    label: str, cache: object, cache_key: str, result: BaseModel, *, capacity: int
) -> None:
    """Write a genuine review outcome back to the cache. Fail-open.

    Preconditions:
        - ``label`` is a short prefix for log messages. ``cache`` is an
          already-resolved ``shared.cache.SharedCache``. ``capacity`` is the
          caller's already-resolved (and already ``> 0``) review-cache
          capacity.
    Postconditions:
        - The entry is written best-effort. Any backend error is caught and
          logged, never propagated -- a broken cache backend never blocks a
          caller from returning its result.
    """
    payload = result.model_dump_json().encode("utf-8")
    try:
        cache.set(cache_key, payload, max_entries=capacity)
    except Exception:
        logger.warning(
            "%s: review cache set failed; continuing without cache write", label, exc_info=True
        )
    else:
        logger.info(
            "%s: cached review result under key=%s (bytes=%d)", label, cache_key, len(payload)
        )


__all__ = [
    "cache_namespace_for",
    "cache_capacity_for",
    "build_review_cache_key",
    "clear_review_cache_namespace",
    "get_cached_review_result",
    "set_cached_review_result",
]
