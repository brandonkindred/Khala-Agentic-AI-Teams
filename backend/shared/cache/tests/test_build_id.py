"""Tests for ``shared.cache.build_id`` namespace suffixing."""

from __future__ import annotations

import pytest

from shared.cache import cache_build_id, with_cache_build_id
from shared.cache.build_id import cache_build_id as cache_build_id_direct


def test_cache_build_id_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KHALA_CACHE_BUILD_ID", raising=False)
    monkeypatch.delenv("KHALA_BUILD_ID", raising=False)
    assert cache_build_id() == ""
    assert with_cache_build_id("cr:chunk:v2") == "cr:chunk:v2"


def test_cache_build_id_prefers_cache_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHALA_CACHE_BUILD_ID", "deploy-abc")
    monkeypatch.setenv("KHALA_BUILD_ID", "ignored")
    assert cache_build_id() == "deploy-abc"
    assert with_cache_build_id("cr:chunk:v2") == "cr:chunk:v2:deploy-abc"


def test_cache_build_id_falls_back_to_build_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KHALA_CACHE_BUILD_ID", raising=False)
    monkeypatch.setenv("KHALA_BUILD_ID", "sha-deadbeef")
    assert cache_build_id_direct() == "sha-deadbeef"


@pytest.mark.parametrize("bad", ["has:colon", "has space", "has$dollar", ""])
def test_cache_build_id_rejects_unsafe(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("KHALA_BUILD_ID", bad if bad else "   ")
    assert cache_build_id() == ""
    assert with_cache_build_id("cr:sub:v1") == "cr:sub:v1"


def test_with_cache_build_id_rejects_empty_base() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        with_cache_build_id("")
