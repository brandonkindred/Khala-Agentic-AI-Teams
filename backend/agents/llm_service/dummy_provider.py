"""Shared helper for forcing every LLM resolution path onto the dummy provider.

Extracted from two call sites that independently converged on the identical
fix: ``branding_team/tests/test_agents.py``'s ``force_dummy_llm`` pytest
fixture and ``branding_team/scripts/eval_selective_context.py``'s eval
script. Both needed to guarantee no live LLM call or Postgres round-trip
happens under a forced dummy provider, even when ``POSTGRES_HOST`` is set
and holds a live provider selection.

Deliberately not named/placed as ``llm_service/testing.py``: this repo's
``testing.py`` modules (``agent_cognition/testing.py``,
``agent_platform/studio/testing.py``, ``shared/postgres/testing.py``) are an
established convention for test-doubles-only code that production code must
never import. ``force_dummy_llm_provider`` is imported by the eval script
above, which is offline/dev tooling, not test code, so it lives in its own
module instead of overloading that convention.
"""

from __future__ import annotations

import contextlib
import os
from typing import Callable, Optional

from . import config as _llm_config
from . import factory as _llm_factory
from . import provider_store as _llm_provider_store


@contextlib.contextmanager
def force_dummy_llm_provider():
    """Force every agent constructed inside this block through the dummy stub client.

    Every ``resolve_*`` function in ``llm_service.config`` (provider, model,
    base URL, API keys) funnels through the single ``_runtime(key)``
    chokepoint, which -- when ``POSTGRES_HOST`` is set -- round-trips
    Postgres via ``runtime_config.get_runtime`` (including a
    ``CREATE TABLE IF NOT EXISTS`` the first time). Setting ``LLM_PROVIDER``
    alone, or even overriding ``resolve_provider`` itself, is not enough:
    ``resolve_model_for_provider`` falls through to ``resolve_model`` for
    every non-Claude provider (dummy included), which calls ``_runtime``
    for the Ollama model key unconditionally, regardless of the active
    provider. Blanking ``_runtime`` itself is the only chokepoint that
    actually stops every one of these resolvers from touching Postgres.

    With ``_runtime`` blanked, ``resolve_provider()`` falls through to the
    ``LLM_PROVIDER`` env var, so that is also pinned to ``"dummy"`` here
    (restored on exit) rather than relying on a caller's ``os.environ``
    state, which a mere ``os.environ.setdefault`` would not override if
    already set to something else.

    Separately, ``get_strands_model`` (``llm_service/strands_provider.py``)
    unconditionally calls ``provider_store.list_fingerprint()`` -- regardless
    of the resolved provider -- to fold the provider list's structural
    fingerprint into the Strands model cache key; that also round-trips
    Postgres when configured. Blank ``load_ordered_entries`` too, and clear
    the Strands-model / LLM-client caches on entry so a warm adapter from
    before this override took effect can't leak through.

    ``_clear_strands_model_cache_for_testing`` is imported lazily (inside
    this function, not at module scope) because ``strands_provider`` imports
    the optional ``strands-agents`` package at import time;
    ``llm_service/__init__.py`` resolves it the same way (via a PEP 562
    ``__getattr__``) so that importing ``llm_service`` -- or, transitively,
    this module -- never pulls Strands into ``sys.modules`` until a Strands
    code path actually runs. That import is attempted in its own
    ``try/except ImportError``, separate from the core patches below: when
    ``strands-agents`` is not installed, ``clear_strands_cache`` is simply
    left ``None`` (skipped, both here and in ``finally``) rather than
    letting the ``ImportError`` propagate and abort before the ``_runtime``/
    ``load_ordered_entries``/``LLM_PROVIDER`` patches -- which have nothing
    to do with Strands and must always apply -- ever run.

    Preconditions:
        None.
    Postconditions:
        ``llm_service.config._runtime``, ``LLM_PROVIDER``, and
        ``llm_service.provider_store.load_ordered_entries`` are all restored
        to their original values on exit, even if the wrapped block raises
        -- so importing or unit-testing this module never leaves
        process-wide LLM provider/config resolution permanently patched for
        unrelated code (e.g. other tests in the same pytest session). This
        holds even if a setup step itself raises (e.g. a cache-clear call):
        every mutation happens inside the ``try:``, after only the
        original-value snapshots are read, so ``finally`` always runs once
        any mutation has. This also holds when the optional ``strands-agents``
        package is not installed: the core patches apply unconditionally
        regardless of whether that import succeeds.
    """
    original_runtime = _llm_config._runtime
    original_load_ordered_entries = _llm_provider_store.load_ordered_entries
    original_provider_env = os.environ.get("LLM_PROVIDER")
    clear_strands_cache: Optional[Callable[[], None]] = None
    try:
        from .strands_provider import _clear_strands_model_cache_for_testing as clear_strands_cache
    except ImportError:
        clear_strands_cache = None
    try:
        _llm_config._runtime = lambda _key: ""
        _llm_provider_store.load_ordered_entries = lambda *args, **kwargs: []
        os.environ["LLM_PROVIDER"] = "dummy"
        _llm_factory.clear_client_cache()
        if clear_strands_cache is not None:
            clear_strands_cache()
        yield
    finally:
        _llm_config._runtime = original_runtime
        _llm_provider_store.load_ordered_entries = original_load_ordered_entries
        if original_provider_env is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = original_provider_env
        _llm_factory.clear_client_cache()
        if clear_strands_cache is not None:
            clear_strands_cache()
