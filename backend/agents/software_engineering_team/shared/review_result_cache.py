"""Shared, parameterized review-result cache for software_engineering_team agents.

``qa_agent``, ``security_agent``, and ``devops_team``'s ``devsecops_review_agent``
each hand-roll the same cache wrapper around
``shared.cache.pydantic_cache``'s primitives: a namespace stem, an env-var-driven
capacity (0 disables the cache), a whole-input-plus-resolved-model cache key, and
a get/validate/corrupt-entry-delete/set/clear policy. This module is the shared,
instantiable version of that wrapper (the sibling of
``devops_team._agent_template.DevOpsSingleShotAgent``'s built-in cache handling
and ``branding_team.shared.phase_output_cache.PhaseOutputCache``, but usable by
any caller that already has a Pydantic input model, a model fingerprint string,
and a Pydantic output model -- no LLM-calling responsibility of its own).

Migrating ``qa_agent``/``security_agent``/``devsecops_review_agent`` onto this
class, and adding review-result caching to ``accessibility_agent`` (which
currently has none), are tracked as separate follow-up work -- this module only
adds the shared primitive.

Namespace/backend resolution mirrors ``DevOpsSingleShotAgent.run`` and
``PhaseOutputCache``: both are re-resolved on every call (not cached on
``self``), so a namespace's build-id suffix (``cache_namespace_for``, via
``KHALA_BUILD_ID``/``KHALA_CACHE_BUILD_ID``) always reflects the environment at
call time rather than at construction time -- this matters for tests, which
routinely change those env vars between constructing an agent/cache and issuing
calls.
"""

from __future__ import annotations

from typing import Generic, Optional, Type, TypeVar

from pydantic import BaseModel

from shared.cache import get_shared_cache
from shared.cache.pydantic_cache import (
    build_model_cache_key,
    cache_capacity_for,
    cache_namespace_for,
    clear_cache_namespace,
    get_cached_model,
    set_cached_model,
)

TOutput = TypeVar("TOutput", bound=BaseModel)

__all__ = ["ReviewResultCache"]


class ReviewResultCache(Generic[TOutput]):
    """A parameterized view over ``shared.cache`` for one review-result cache.

    Generalizes the cache wrapper ``qa_agent``, ``security_agent``, and
    ``devsecops_review_agent`` each reimplement: a namespace stem, an
    env-var-driven capacity (0 disables the cache), a whole-input-plus-model
    cache key (``build_model_cache_key``), and the get/validate/corrupt-delete/
    set/clear policy in ``shared.cache.pydantic_cache``.

    Invariants:
        - Storage is namespaced by ``cache_namespace_for(namespace_stem)``,
          which appends the current build id -- a deploy is a disjoint
          keyspace, no manual invalidation needed.
        - The underlying backend is a process-wide singleton per resolved
          namespace (``shared.cache.get_shared_cache``), not private to this
          instance: two ``ReviewResultCache`` instances constructed with the
          same ``namespace_stem`` share every entry, and clearing one clears
          both's view.
        - Every operation is fail-open: a cache backend error is caught and
          logged (never raised) by the ``shared.cache.pydantic_cache``
          primitives this class delegates to. ``get`` never raises for a
          missing or corrupt entry (a corrupt entry is deleted and treated as
          a miss); ``put``/``clear`` never raise for a backend failure.
        - ``get`` always attempts a lookup, independent of capacity --
          capacity only gates ``put`` (an existing entry written while
          capacity was positive remains readable after the env var is later
          set to ``0``, matching ``SharedCache.set``'s "0 means do not store"
          contract rather than "0 means never read"). Every existing
          per-agent wrapper reads the same capacity value already resolved
          for the following write and so incidentally skips the read too when
          disabled; this class treats ``get``/``put`` as independent public
          operations and does not fuse that optimization in, per the issue's
          separately-specified `get`/`put` contracts.
    """

    def __init__(
        self,
        namespace_stem: str,
        env_var: str,
        default_capacity: int,
        label: str,
        output_model: Type[TOutput],
    ) -> None:
        """Configure (without yet resolving) one review-result cache.

        Preconditions:
            - ``namespace_stem`` is a non-empty cache namespace stem (e.g.
              ``"qa:review:v1"``), unique per logical cache -- two callers
              sharing a stem share the same entries.
            - ``env_var`` is the environment variable name that controls this
              cache's capacity (e.g. ``"QA_REVIEW_CACHE_SIZE"``).
            - ``default_capacity`` is the capacity used when ``env_var`` is
              unset or unparseable; ``>= 0``.
            - ``label`` is a short prefix for log messages (e.g. ``"QA"``).
            - ``output_model`` is the Pydantic model type cached results are
              validated against on read.
        Postconditions:
            - No cache backend is resolved yet (resolution is per-call, see
              class docstring); construction never raises for a backend
              failure since no backend is touched.
        """
        assert namespace_stem, "namespace_stem is required"
        assert env_var, "env_var is required"
        assert label, "label is required"
        assert output_model is not None, "output_model is required"
        self._namespace_stem = namespace_stem
        self._env_var = env_var
        self._default_capacity = default_capacity
        self._label = label
        self._output_model = output_model

    def _namespace(self) -> str:
        return cache_namespace_for(self._namespace_stem)

    def capacity(self) -> int:
        """Resolve this cache's current capacity from the environment.

        Preconditions:
            - None.
        Postconditions:
            - Returns ``cache_capacity_for(env_var, default_capacity)``,
              resolved fresh on this call (not cached on ``self``), so a
              caller that wants ``get``/``put`` fused into a single
              disabled-means-never-read-or-write policy (the convention every
              pre-``ReviewResultCache`` per-agent wrapper used) can gate its
              own ``get`` call on ``capacity() > 0`` before calling it --
              see the class docstring's note on why ``get`` itself does not
              fuse that gate in.
        """
        return cache_capacity_for(self._env_var, self._default_capacity)

    def get(self, input_data: BaseModel, model_fp: str) -> Optional[TOutput]:
        """Look up the cached result for ``input_data``/``model_fp``.

        Preconditions:
            - ``input_data`` is a Pydantic model instance whose own fields
              never include a top-level ``__model__`` key.
            - ``model_fp`` is a stable identifier for the resolved model
              (e.g. from ``llm_service.strands_model.model_fingerprint``).
        Postconditions:
            - Returns a validated ``output_model`` instance equal to
              whatever was last stored via ``put(input_data, model_fp,
              result)`` for the same pair (a hit), or ``None`` on a miss, a
              cache backend error, or a corrupt entry (deleted so it never
              masks the same key again). Never raises.
        """
        cache_key = build_model_cache_key(input_data, model_fp)
        cache = get_shared_cache(self._namespace())
        return get_cached_model(self._label, cache, cache_key, self._output_model)

    def put(self, input_data: BaseModel, model_fp: str, result: BaseModel) -> None:
        """Store ``result`` for ``input_data``/``model_fp``, respecting capacity.

        Preconditions:
            - ``input_data``/``model_fp`` are the same values that will be
              passed to a later ``get`` to retrieve this entry.
            - ``result`` is an instance of (or otherwise serializes/validates
              as) this cache's ``output_model``.
        Postconditions:
            - When the configured capacity (``cache_capacity_for(env_var,
              default_capacity)``, resolved fresh on this call) is ``<= 0``,
              this is a no-op -- caching is disabled, matching the
              ``capacity <= 0`` passthrough convention used by every existing
              caller.
            - Otherwise, ``result`` is written best-effort (fail-open on any
              backend error, never raises); a subsequent ``get(input_data,
              model_fp)`` then returns an equal value until evicted by the
              shared backend's LRU (bounded by that capacity) or ``clear()``.
        """
        capacity = cache_capacity_for(self._env_var, self._default_capacity)
        if capacity <= 0:
            return
        cache_key = build_model_cache_key(input_data, model_fp)
        cache = get_shared_cache(self._namespace())
        set_cached_model(self._label, cache, cache_key, result, capacity=capacity)

    def clear(self) -> None:
        """Drop every cached entry in this cache's namespace.

        Preconditions:
            - None.
        Postconditions:
            - This process's view of the namespace is empty (best-effort
              across Redis) when this returns. Any cache backend error is
              caught and logged, never propagated -- intended for test
              teardown / forced cold runs.
        """
        clear_cache_namespace(self._label, lambda: get_shared_cache(self._namespace()))
