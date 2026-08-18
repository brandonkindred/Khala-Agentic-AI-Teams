"""Shared single-shot LLM-response cache wiring, keyed on a whole Pydantic input.

Several single-shot review/design agents across this team cache one whole
structured input → whole structured output pair: hash the entire Pydantic
input model plus the resolved review model, check ``shared.cache`` before
calling the LLM, and write the result back after a genuine (non-fallback)
call. ``qa_agent.agent.QAExpertAgent.run`` and every ``devops_team``
specialist agent that makes its own single-shot LLM call share this exact
shape; this module factors it out so each consumer only wires four small
constants (namespace stem, env var name, default capacity, output model)
instead of re-deriving the hashing/get/set/fail-open boilerplate by hand.

Not every single-shot cache in this team uses this module: the V2 tool-agent
review cache (``shared/llm_tool_agent_base.py``) keys on the concrete class
identity plus the *rendered prompt* rather than a structured input model
(tool agents don't share one common Pydantic input shape), so it has its own
narrower implementation.

Invariants:
    - No function here ever raises for a cache-backend failure (Redis down,
      corrupt entry, etc.) — every operation is fail-open, matching
      ``shared.cache``'s own contract plus a belt-and-braces local
      ``try/except``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from shared.cache import get_shared_cache
from shared.env_config import env_int

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def cache_namespace(stem: str) -> str:
    """Resolve a cache namespace stem to its build-id-suffixed form.

    Preconditions:
        - ``stem`` is a non-empty namespace stem, e.g. ``"devops:iac:v1"``.
    Postconditions:
        - Returns ``stem`` with the current cache build id appended (see
          ``shared.cache.with_cache_build_id``), so a deploy is automatically
          a cold cache. Lazily imported to avoid an import cycle.
    """
    from shared.cache import with_cache_build_id  # noqa: PLC0415

    return with_cache_build_id(stem)


def cache_capacity(env_var: str, default: int) -> int:
    """Resolve a cache's entry capacity from the environment.

    Preconditions:
        - ``env_var`` is a non-empty environment variable name; ``default``
          is a non-negative int.
    Postconditions:
        - Returns ``env_var`` parsed as an int, clamped to a floor of 0: an
          unset or unparseable value falls back to ``default``, a negative
          value clamps to 0. An explicit or clamped-to 0 disables that cache
          — every call re-invokes the LLM, matching pre-cache behavior.
    """
    return env_int(env_var, default, 0)


def build_cache_key(input_data: BaseModel, model_fp: str) -> str:
    """Hash of the whole input model plus the resolved review model.

    Same key design as ``code_review_agent.mapping._submission_fingerprint``:
    keys the entire input model so any field change naturally busts the key
    with no explicit invalidation logic to maintain.

    Preconditions:
        - ``input_data`` is a valid Pydantic model instance.
        - ``model_fp`` is the value returned by
          ``llm_service.strands_model.model_fingerprint`` for the resolved
          Strands model this call will use.
    Postconditions:
        - Returns a hex digest that changes whenever any input field or the
          resolved model changes, and is stable (``sort_keys``) across calls.
    """
    payload = input_data.model_dump(mode="json")
    payload["__model__"] = model_fp
    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def get_cached_result(
    namespace_stem: str, key: str, output_cls: Type[T], *, log_prefix: str
) -> Optional[T]:
    """Look up a cached result, failing open to a miss on any error.

    Preconditions:
        - ``namespace_stem`` and ``key`` are non-empty; ``output_cls`` is the
          Pydantic model type the cached payload should validate against.
    Postconditions:
        - Returns the validated ``output_cls`` instance on a hit. Returns
          ``None`` on a miss, a cache-backend error (logged, not raised), or
          a corrupt entry (logged; the entry is evicted via ``cache.delete``
          before returning ``None`` so a future call starts clean).
    """
    cache = get_shared_cache(cache_namespace(namespace_stem))
    try:
        raw = cache.get(key)
    except Exception:
        logger.warning("%s: cache get failed; treating as miss", log_prefix, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return output_cls.model_validate_json(raw)
    except Exception:
        logger.warning(
            "%s: corrupt cache entry for %s; treating as miss", log_prefix, key, exc_info=True
        )
        try:
            cache.delete(key)
        except Exception:
            logger.warning("%s: cache delete failed after corrupt entry", log_prefix, exc_info=True)
        return None


def set_cached_result(
    namespace_stem: str, key: str, result: BaseModel, capacity: int, *, log_prefix: str
) -> None:
    """Write a genuine (non-fallback) result back to the cache, failing open.

    Preconditions:
        - ``namespace_stem`` and ``key`` are non-empty; ``capacity`` is the
          same value used to decide whether to cache at all (``> 0``).
    Postconditions:
        - The result is stored under ``key`` on success. Any cache-backend
          error is logged and swallowed — the caller's result is unaffected
          either way, since this is called only after the result is final.
    """
    cache = get_shared_cache(cache_namespace(namespace_stem))
    payload = result.model_dump_json().encode("utf-8")
    try:
        cache.set(key, payload, max_entries=capacity)
    except Exception:
        logger.warning(
            "%s: cache set failed; continuing without cache write", log_prefix, exc_info=True
        )
    else:
        logger.info("%s: cached result under key=%s (bytes=%d)", log_prefix, key, len(payload))


def clear_cache(namespace_stem: str, *, log_prefix: str) -> None:
    """Drop every cached entry in a namespace. Intended for test teardown.

    Preconditions:
        - ``namespace_stem`` is non-empty.
    Postconditions:
        - This process's view of the namespace is empty when the call
          returns (best-effort across Redis). A cache backend error is
          caught and logged rather than propagated — fails open, same as
          every other operation in this module.
    """
    try:
        get_shared_cache(cache_namespace(namespace_stem)).clear()
    except Exception:
        logger.warning("%s: cache clear failed", log_prefix, exc_info=True)


__all__ = [
    "build_cache_key",
    "cache_capacity",
    "cache_namespace",
    "clear_cache",
    "get_cached_result",
    "set_cached_result",
]
