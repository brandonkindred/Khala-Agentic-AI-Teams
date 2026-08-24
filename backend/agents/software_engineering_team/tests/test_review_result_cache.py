"""Unit tests for the shared ``ReviewResultCache`` (backend/.../shared/review_result_cache.py).

Pure unit tests of the cache class itself, in isolation from any LLM agent:
constructs ``ReviewResultCache`` instances directly against a small dummy
output model and exercises ``get``/``put``/``clear``. Each test (and each
``ReviewResultCache`` instance within a test that needs two) uses its own
freshly-generated namespace stem (see ``_unique_stem``) so tests never share
backend entries and need no dedicated autouse reset fixture beyond the
existing SE-team-wide ``_reset_code_review_chunk_cache`` autouse fixture in
``conftest.py``, which already clears ``KHALA_BUILD_ID``/
``KHALA_CACHE_BUILD_ID`` before every test in this package (so
``cache_namespace_for`` is deterministic here too).
"""

from __future__ import annotations

import itertools

import pytest
from pydantic import BaseModel

from shared.cache import get_shared_cache
from shared.cache.pydantic_cache import build_model_cache_key, cache_namespace_for
from software_engineering_team.shared.review_result_cache import ReviewResultCache

_ENV_VAR = "TEST_REVIEW_RESULT_CACHE_SIZE"
_LABEL = "TestReviewResultCache"
_stem_counter = itertools.count()


def _unique_stem() -> str:
    """A namespace stem guaranteed disjoint from every other call in this process."""
    return f"test:review-result-cache:{next(_stem_counter)}:v1"


class _DummyInput(BaseModel):
    value: str


class _DummyOutput(BaseModel):
    result: str


def _cache(default_capacity: int = 8, env_var: str = _ENV_VAR) -> ReviewResultCache[_DummyOutput]:
    return ReviewResultCache(_unique_stem(), env_var, default_capacity, _LABEL, _DummyOutput)


def test_get_on_empty_cache_is_a_miss() -> None:
    cache = _cache()
    assert cache.get(_DummyInput(value="a"), "model-1") is None


def test_put_then_get_round_trips() -> None:
    cache = _cache()
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")

    cache.put(input_data, "model-1", result)

    assert cache.get(input_data, "model-1") == result


def test_get_distinguishes_by_input_and_model_fp() -> None:
    cache = _cache()
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")
    cache.put(input_data, "model-1", result)

    # Different input -> miss.
    assert cache.get(_DummyInput(value="b"), "model-1") is None
    # Same input, different resolved model -> miss.
    assert cache.get(input_data, "model-2") is None


def test_put_respects_default_capacity_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var set -> falls back to ``default_capacity``, so entries persist."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    cache = _cache(default_capacity=8)
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")

    cache.put(input_data, "model-1", result)

    assert cache.get(input_data, "model-1") == result


def test_put_capacity_zero_via_default_disables_cache() -> None:
    """``default_capacity=0`` (with no env var override) disables writes."""
    cache = _cache(default_capacity=0)
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")

    cache.put(input_data, "model-1", result)

    assert cache.get(input_data, "model-1") is None


def test_put_capacity_zero_via_env_var_disables_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting the env var to ``0`` disables writes even with a positive default."""
    cache = _cache(default_capacity=8)
    monkeypatch.setenv(_ENV_VAR, "0")
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")

    cache.put(input_data, "model-1", result)

    assert cache.get(input_data, "model-1") is None


def test_env_var_override_takes_precedence_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positive env var overrides a zero default, re-enabling the cache."""
    cache = _cache(default_capacity=0)
    monkeypatch.setenv(_ENV_VAR, "8")
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")

    cache.put(input_data, "model-1", result)

    assert cache.get(input_data, "model-1") == result


def test_put_respects_capacity_eviction() -> None:
    """With capacity=1, a second distinct entry evicts the first."""
    cache = _cache(default_capacity=1)
    first_input = _DummyInput(value="a")
    second_input = _DummyInput(value="b")
    first_result = _DummyOutput(result="first")
    second_result = _DummyOutput(result="second")

    cache.put(first_input, "model-1", first_result)
    cache.put(second_input, "model-1", second_result)

    assert cache.get(first_input, "model-1") is None
    assert cache.get(second_input, "model-1") == second_result


def test_clear_wipes_entries() -> None:
    cache = _cache()
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")
    cache.put(input_data, "model-1", result)
    assert cache.get(input_data, "model-1") == result

    cache.clear()

    assert cache.get(input_data, "model-1") is None


def test_clear_on_empty_cache_is_a_no_op() -> None:
    cache = _cache()
    cache.clear()  # must not raise


def test_clear_falls_open_on_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``clear()`` never raises, even when the backend does."""
    import software_engineering_team.shared.review_result_cache as module

    class _RaisingCache:
        def clear(self) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(module, "get_shared_cache", lambda namespace: _RaisingCache())

    cache = _cache()
    cache.clear()  # must not raise


def test_get_falls_open_on_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``get`` backend error is treated as a miss, never raised."""
    import software_engineering_team.shared.review_result_cache as module

    class _RaisingCache:
        def get(self, key: str) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(module, "get_shared_cache", lambda namespace: _RaisingCache())

    cache = _cache()
    assert cache.get(_DummyInput(value="a"), "model-1") is None


def test_put_falls_open_on_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``put`` backend error is swallowed, never raised."""
    import software_engineering_team.shared.review_result_cache as module

    class _RaisingCache:
        def set(self, key: str, value: bytes, *, max_entries: int) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(module, "get_shared_cache", lambda namespace: _RaisingCache())

    cache = _cache()
    cache.put(_DummyInput(value="a"), "model-1", _DummyOutput(result="ok"))  # must not raise


def test_corrupt_cache_entry_is_treated_as_miss_and_deleted() -> None:
    """A cache entry that fails to validate against the output model is a miss
    and is deleted so it never masks the key again."""
    stem = _unique_stem()
    cache = ReviewResultCache(stem, _ENV_VAR, 8, _LABEL, _DummyOutput)
    input_data = _DummyInput(value="a")

    # Write raw garbage directly through the resolved backend, bypassing
    # ReviewResultCache.put (which only ever writes valid payloads).
    namespace = cache_namespace_for(stem)
    backend = get_shared_cache(namespace)
    key = build_model_cache_key(input_data, "model-1")
    backend.set(key, b"not valid json", max_entries=8)

    assert cache.get(input_data, "model-1") is None
    # The corrupt entry was deleted, not merely shadowed.
    assert backend.get(key) is None


def test_two_instances_with_different_namespaces_are_isolated() -> None:
    cache_a = _cache()
    cache_b = _cache()
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")

    cache_a.put(input_data, "model-1", result)

    assert cache_a.get(input_data, "model-1") == result
    assert cache_b.get(input_data, "model-1") is None


def test_two_instances_with_same_namespace_share_entries() -> None:
    """Same namespace stem -> same process-wide singleton backend (documented
    invariant), so a second instance sees the first's writes."""
    stem = _unique_stem()
    cache_a = ReviewResultCache(stem, _ENV_VAR, 8, _LABEL, _DummyOutput)
    cache_b = ReviewResultCache(stem, _ENV_VAR, 8, _LABEL, _DummyOutput)
    input_data = _DummyInput(value="a")
    result = _DummyOutput(result="ok")

    cache_a.put(input_data, "model-1", result)

    assert cache_b.get(input_data, "model-1") == result
