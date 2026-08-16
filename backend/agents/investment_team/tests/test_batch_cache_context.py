"""Tests for ``strategy_lab.batch_cache_context`` — the process-local batch
indicator-cache registry, the active-cache ContextVar/binding, and the
``new_registry`` executor helper.

These cover the *wiring* the batch workflow relies on: one shared
``BatchIndicatorCache`` instance per batch key (resolved process-locally because
the instance can't cross a Temporal boundary), LRU eviction of idle keys, and
that ``new_registry`` hands the bound instance to every ``IndicatorRegistry``
built inside a batch attempt only when the feature flag is on. Concurrency /
cross-strategy-correctness coverage is a separate sub-issue.
"""

from __future__ import annotations

import pytest

from investment_team.strategy_lab import batch_cache_context as bcc
from investment_team.strategy_lab.batch_indicator_cache import BatchIndicatorCache

_ENV_VAR = "STRATEGY_LAB_BATCH_INDICATOR_CACHE_ENABLED"


@pytest.fixture(autouse=True)
def _clear_process_caches():
    """Isolate each test from the module-global cache registry + binding."""
    bcc._caches.clear()
    yield
    bcc._caches.clear()


# ---------------------------------------------------------------------------
# get_or_create_batch_cache — identity + eviction
# ---------------------------------------------------------------------------


def test_same_key_returns_same_instance():
    a = bcc.get_or_create_batch_cache("run-1-b0")
    b = bcc.get_or_create_batch_cache("run-1-b0")
    assert a is b
    assert isinstance(a, BatchIndicatorCache)


def test_distinct_keys_return_distinct_instances():
    a = bcc.get_or_create_batch_cache("run-1-b0")
    c = bcc.get_or_create_batch_cache("run-1-b1")
    assert a is not c


def test_empty_key_rejected():
    with pytest.raises(AssertionError):
        bcc.get_or_create_batch_cache("")


def test_lru_evicts_least_recently_used(monkeypatch):
    monkeypatch.setattr(bcc, "_MAX_CACHES", 2)
    a = bcc.get_or_create_batch_cache("k0")
    bcc.get_or_create_batch_cache("k1")
    # Touch k0 so k1 becomes the least-recently-used entry.
    assert bcc.get_or_create_batch_cache("k0") is a
    # Adding a third key evicts k1 (LRU), not the freshly-touched k0.
    bcc.get_or_create_batch_cache("k2")
    assert bcc.get_or_create_batch_cache("k0") is a  # survived
    # k1 was evicted → a re-request builds a brand-new instance.
    assert "k1" not in bcc._caches


# ---------------------------------------------------------------------------
# use_batch_indicator_cache / active_batch_indicator_cache — bind + restore
# ---------------------------------------------------------------------------


def test_binding_is_visible_then_restored():
    cache = BatchIndicatorCache()
    assert bcc.active_batch_indicator_cache() is None
    with bcc.use_batch_indicator_cache(cache):
        assert bcc.active_batch_indicator_cache() is cache
    assert bcc.active_batch_indicator_cache() is None


def test_binding_restored_on_exception():
    cache = BatchIndicatorCache()
    with pytest.raises(ValueError):
        with bcc.use_batch_indicator_cache(cache):
            assert bcc.active_batch_indicator_cache() is cache
            raise ValueError("boom")
    assert bcc.active_batch_indicator_cache() is None


# ---------------------------------------------------------------------------
# new_registry — reads the binding, honors the feature flag
# ---------------------------------------------------------------------------


def test_new_registry_unbound_has_no_cache(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "false")
    reg = bcc.new_registry()
    assert reg._batch_cache is None


def test_new_registry_bound_with_flag_on_shares_instance(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    with bcc.use_batch_indicator_cache(cache):
        assert bcc.new_registry()._batch_cache is cache


def test_new_registry_bound_with_flag_unset_shares_instance(monkeypatch):
    """The flag now defaults on, so a bound cache is shared with the env var
    unset — the flipped bake-in default."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    cache = BatchIndicatorCache()
    with bcc.use_batch_indicator_cache(cache):
        assert bcc.new_registry()._batch_cache is cache


def test_new_registry_bound_with_flag_off_is_inert(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "false")
    cache = BatchIndicatorCache()
    with bcc.use_batch_indicator_cache(cache):
        # The registry ctor nulls batch_cache when the flag is off, so a bound
        # instance is inert once the flag is explicitly disabled.
        assert bcc.new_registry()._batch_cache is None
