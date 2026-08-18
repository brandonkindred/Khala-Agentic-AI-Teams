"""Unit tests for ``shared.cache.pydantic_cache``'s six primitives.

Scoped to what's new in this module: the primitives' own get/validate/
corrupt-delete/set/clear logic and fail-open branches. ``with_cache_build_id``
and ``env_int`` (which ``cache_namespace_for``/``cache_capacity_for``
delegate to) and ``SharedCache``'s own contract already have full coverage
in ``test_build_id.py`` and ``test_shared_cache.py`` respectively -- this
file only proves the delegation, not every edge case of those.
"""

from __future__ import annotations

from typing import List, NoReturn, Optional, Tuple

import pytest
from pydantic import BaseModel

from shared.cache import MemoryBackend
from shared.cache.pydantic_cache import (
    build_model_cache_key,
    cache_capacity_for,
    cache_namespace_for,
    clear_cache_namespace,
    get_cached_model,
    set_cached_model,
)


class _Widget(BaseModel):
    name: str
    count: int = 0


class _StubCache:
    """Minimal cache stub whose methods can be made to raise, for fail-open tests."""

    def __init__(self, *, raise_on: Tuple[str, ...] = (), get_return: Optional[bytes] = None) -> None:
        self._raise_on = set(raise_on)
        self._get_return = get_return
        self.set_calls: List[Tuple[str, bytes, int]] = []
        self.delete_calls: List[str] = []
        self.clear_calls = 0

    def get(self, key: str) -> Optional[bytes]:
        if "get" in self._raise_on:
            raise RuntimeError("boom")
        return self._get_return

    def set(self, key: str, value: bytes, *, max_entries: int) -> None:
        if "set" in self._raise_on:
            raise RuntimeError("boom")
        self.set_calls.append((key, value, max_entries))

    def delete(self, key: str) -> None:
        if "delete" in self._raise_on:
            raise RuntimeError("boom")
        self.delete_calls.append(key)

    def clear(self) -> None:
        if "clear" in self._raise_on:
            raise RuntimeError("boom")
        self.clear_calls += 1


# ---------------------------------------------------------------------------
# cache_namespace_for
# ---------------------------------------------------------------------------


def test_cache_namespace_for_unchanged_without_build_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KHALA_CACHE_BUILD_ID", raising=False)
    monkeypatch.delenv("KHALA_BUILD_ID", raising=False)
    assert cache_namespace_for("widget:v1") == "widget:v1"


def test_cache_namespace_for_suffixes_with_build_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHALA_CACHE_BUILD_ID", "abc123")
    assert cache_namespace_for("widget:v1") == "widget:v1:abc123"


# ---------------------------------------------------------------------------
# cache_capacity_for
# ---------------------------------------------------------------------------


def test_cache_capacity_for_unset_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIDGET_CACHE_SIZE", raising=False)
    assert cache_capacity_for("WIDGET_CACHE_SIZE", 42) == 42


def test_cache_capacity_for_uses_parsed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIDGET_CACHE_SIZE", "7")
    assert cache_capacity_for("WIDGET_CACHE_SIZE", 42) == 7


def test_cache_capacity_for_clamps_negative_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIDGET_CACHE_SIZE", "-5")
    assert cache_capacity_for("WIDGET_CACHE_SIZE", 42) == 0


def test_cache_capacity_for_unparseable_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIDGET_CACHE_SIZE", "not-an-int")
    assert cache_capacity_for("WIDGET_CACHE_SIZE", 42) == 42


# ---------------------------------------------------------------------------
# build_model_cache_key
# ---------------------------------------------------------------------------


def test_build_model_cache_key_same_input_same_key() -> None:
    widget = _Widget(name="a", count=1)
    assert build_model_cache_key(widget, "model-fp-1") == build_model_cache_key(widget, "model-fp-1")


def test_build_model_cache_key_changed_field_changes_key() -> None:
    key1 = build_model_cache_key(_Widget(name="a", count=1), "model-fp-1")
    key2 = build_model_cache_key(_Widget(name="a", count=2), "model-fp-1")
    assert key1 != key2


def test_build_model_cache_key_changed_model_fp_changes_key() -> None:
    widget = _Widget(name="a", count=1)
    key1 = build_model_cache_key(widget, "model-fp-1")
    key2 = build_model_cache_key(widget, "model-fp-2")
    assert key1 != key2


# ---------------------------------------------------------------------------
# get_cached_model
# ---------------------------------------------------------------------------


def test_get_cached_model_miss_on_empty_cache() -> None:
    cache = MemoryBackend()
    assert get_cached_model("Widget", cache, "missing-key", _Widget) is None


def test_get_cached_model_hit_returns_validated_model() -> None:
    cache = MemoryBackend()
    widget = _Widget(name="a", count=1)
    cache.set("key-1", widget.model_dump_json().encode("utf-8"), max_entries=8)

    result = get_cached_model("Widget", cache, "key-1", _Widget)

    assert result == widget


def test_get_cached_model_corrupt_entry_is_deleted_and_treated_as_miss() -> None:
    cache = MemoryBackend()
    cache.set("key-1", b"not valid json", max_entries=8)

    assert get_cached_model("Widget", cache, "key-1", _Widget) is None
    # Eviction actually happened -- the key is gone, not just misread.
    assert cache.get("key-1") is None


def test_get_cached_model_get_error_falls_open_to_miss() -> None:
    cache = _StubCache(raise_on=("get",))
    assert get_cached_model("Widget", cache, "key-1", _Widget) is None


def test_get_cached_model_delete_error_after_corrupt_entry_still_falls_open() -> None:
    cache = _StubCache(raise_on=("delete",), get_return=b"not valid json")
    assert get_cached_model("Widget", cache, "key-1", _Widget) is None


# ---------------------------------------------------------------------------
# set_cached_model
# ---------------------------------------------------------------------------


def test_set_cached_model_writes_expected_payload_and_capacity() -> None:
    cache = _StubCache()
    widget = _Widget(name="a", count=1)

    set_cached_model("Widget", cache, "key-1", widget, capacity=16)

    assert cache.set_calls == [("key-1", widget.model_dump_json().encode("utf-8"), 16)]


def test_set_cached_model_set_error_falls_open() -> None:
    cache = _StubCache(raise_on=("set",))
    widget = _Widget(name="a", count=1)

    set_cached_model("Widget", cache, "key-1", widget, capacity=16)  # must not raise


# ---------------------------------------------------------------------------
# clear_cache_namespace
# ---------------------------------------------------------------------------


def test_clear_cache_namespace_clears_resolved_cache() -> None:
    cache = _StubCache()
    clear_cache_namespace("Widget", lambda: cache)
    assert cache.clear_calls == 1


def test_clear_cache_namespace_resolve_error_falls_open() -> None:
    def _raise() -> NoReturn:
        raise RuntimeError("boom")

    clear_cache_namespace("Widget", _raise)  # must not raise


def test_clear_cache_namespace_clear_error_falls_open() -> None:
    cache = _StubCache(raise_on=("clear",))
    clear_cache_namespace("Widget", lambda: cache)  # must not raise
