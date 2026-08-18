"""Shared-cache-backed phase-output cache for branding-team pipeline memoization.

A ``BrandPhase``-keyed cache of ``(input_hash, output)`` entries, backed by
``shared.cache.get_shared_cache`` (Redis when configured, else in-process
memory) instead of a team-local dict. Paired with ``phase_input_hash``
(``shared/memoization.py``), this lets a caller detect an unchanged phase
input and skip re-running that phase. No cache is consumed here — this
module only provides the container and its ``get``/``put`` helpers.
``orchestrator.run`` consumes it (Story 2b) on the thread path only via its
optional ``phase_cache`` parameter — the Temporal path calls
``orchestrator.run_single_phase`` directly and has no cache parameter to
receive one. The conversation/session layer holds a per-conversation
``PhaseOutputCache`` handle across turns via a registry in
``api/conversation.py`` (``_get_or_create_phase_cache``, Story 2c Step 1) and
threads it into ``orchestrator.run`` from both chat call sites via
``_run_orchestrator_if_ready`` (Story 2c Step 2). That handle is a thin view
rather than private state: see below, every instance in a process addresses
the same shared entries, so retaining one across turns is a structural/API
guarantee (the same handle stays available to a future consumer), not a
source of per-conversation cache isolation.

Storage is namespaced (``branding:phase:v1``, suffixed with a build id via
``shared.cache.pydantic_cache.cache_namespace_for`` so a deploy cold-starts
the cache) and keyed by ``f"{phase.value}:{input_hash}"``, so every distinct
``(phase, input_hash)`` pair addresses its own entry rather than sharing one
slot per phase. This means a ``put`` for a phase under a *new* hash does not
evict the *old* hash's entry — entries only leave the cache via the shared
backend's LRU (bounded by ``_MAX_ENTRIES``) or an explicit
``clear_phase_output_cache()``. Because the underlying backend is a
process-wide singleton per namespace (not private to a ``PhaseOutputCache``
instance), every instance in a process shares the same entries.

The get/validate/corrupt-delete/set/clear mechanics are the team-neutral
policy in ``shared.cache.pydantic_cache`` (shared with
``software_engineering_team``'s review-result caches); this module supplies
only its namespace stem, the per-phase output model lookup, and a fixed
capacity (``_MAX_ENTRIES``, not env-var-driven here).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from branding_team.graphs.shared import PHASE_ORDER, PHASE_OUTPUT_MODELS
from branding_team.models import BrandPhase
from shared.cache import get_shared_cache
from shared.cache.pydantic_cache import (
    cache_namespace_for,
    clear_cache_namespace,
    get_cached_model,
    set_cached_model,
)

__all__ = ["PhaseOutputCache", "clear_phase_output_cache"]

# Bounded LRU shared by every PhaseOutputCache instance in this process (and,
# under Redis, across processes). Five phases each address several distinct
# input hashes over a process's lifetime; 64 gives ample headroom without
# needing operator tuning.
_MAX_ENTRIES = 64

# Base stem; ``_phase_cache_namespace()`` appends ``KHALA_CACHE_BUILD_ID`` /
# ``KHALA_BUILD_ID`` when set so a deploy is a cold cache under Redis.
_PHASE_CACHE_NAMESPACE = "branding:phase:v1"


def _phase_cache_namespace() -> str:
    """Shared-cache namespace for branding phase outputs (includes build id)."""
    return cache_namespace_for(_PHASE_CACHE_NAMESPACE)


def _cache_key(phase: BrandPhase, input_hash: str) -> str:
    return f"{phase.value}:{input_hash}"


def clear_phase_output_cache() -> None:
    """Drop every cached phase output.

    Postconditions:
        - This process's view of the shared phase-output namespace is empty
          when this function returns (best-effort across Redis). Intended
          for tests (the cache persists across ``PhaseOutputCache``
          instances by design) and for callers that must force a cold run.
          A cache backend error is caught and logged rather than propagated
          — fails open, so a broken backend never breaks a caller (e.g. a
          test-teardown fixture) forcing a cold run.
    """
    clear_cache_namespace("branding-phase", lambda: get_shared_cache(_phase_cache_namespace()))


class PhaseOutputCache:
    """A ``BrandPhase``-keyed view over the shared ``branding:phase`` cache.

    Invariants:
        - ``get(phase, input_hash)`` returns an output equal to (though not
          necessarily the same object as) whatever was last stored via
          ``put(phase, input_hash, output)`` for that exact pair, until it is
          evicted by the shared backend's LRU or ``clear_phase_output_cache``.
        - No LLM or other side effects: reads and writes go through
          ``shared.cache``, which is itself fail-open (a backend outage
          degrades to a miss/no-op, never an exception).
    """

    @staticmethod
    def _validate_phase(phase: BrandPhase) -> None:
        if phase not in PHASE_ORDER:
            raise ValueError(f"{phase!r} is not a runnable branding phase")

    def get(self, phase: BrandPhase, input_hash: str) -> Optional[BaseModel]:
        """Return the cached output for ``phase``/``input_hash``, or ``None`` on miss.

        Preconditions:
            - ``phase`` is one of the five runnable pipeline phases in
              ``PHASE_ORDER``; ``BrandPhase.COMPLETE`` is not accepted.
            - ``input_hash`` is a hash produced by ``phase_input_hash`` for
              the same ``phase``.
        Postconditions:
            - Returns a value equal to the ``output`` previously stored via
              ``put(phase, input_hash, output)``, when such an entry exists
              and has not been evicted (a hit).
            - Returns ``None`` when no entry exists for ``(phase,
              input_hash)`` (a miss), including when a stored entry's bytes
              fail to deserialize against ``phase``'s output model (a
              corrupt entry is evicted and treated as a miss) — never raises
              for a missing or corrupt entry.
        """
        self._validate_phase(phase)
        cache = get_shared_cache(_phase_cache_namespace())
        key = _cache_key(phase, input_hash)
        model_cls = PHASE_OUTPUT_MODELS[phase]
        return get_cached_model("branding-phase", cache, key, model_cls)

    def put(self, phase: BrandPhase, input_hash: str, output: BaseModel) -> None:
        """Store ``output`` for ``phase``/``input_hash``.

        Preconditions:
            - ``phase`` is one of the five runnable pipeline phases in
              ``PHASE_ORDER``; ``BrandPhase.COMPLETE`` is not accepted.
            - ``input_hash`` is a hash produced by ``phase_input_hash`` for
              the same ``phase``.
            - ``output`` is the phase's constructed output model (an
              instance of ``PHASE_OUTPUT_MODELS[phase]``).
        Postconditions:
            - ``get(phase, input_hash)`` subsequently returns a value equal
              to ``output`` (a hit), until evicted by the shared backend's
              LRU or ``clear_phase_output_cache``.
        """
        self._validate_phase(phase)
        cache = get_shared_cache(_phase_cache_namespace())
        key = _cache_key(phase, input_hash)
        set_cached_model("branding-phase", cache, key, output, capacity=_MAX_ENTRIES)
