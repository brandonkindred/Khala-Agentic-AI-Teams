"""Tests for the cache-breakpoint marking primitive (``llm_service.cache_breakpoint``)."""

from __future__ import annotations

import dataclasses

import pytest

from llm_service.cache_breakpoint import CacheBreakpoint


def test_construction_holds_exact_text() -> None:
    bp = CacheBreakpoint("stable prefix content")
    assert bp.text == "stable prefix content"


def test_is_frozen() -> None:
    bp = CacheBreakpoint("prefix")
    with pytest.raises(dataclasses.FrozenInstanceError):
        bp.text = "other"  # type: ignore[misc]


def test_equal_text_compares_and_hashes_equal() -> None:
    a = CacheBreakpoint("same")
    b = CacheBreakpoint("same")
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_different_text_not_equal() -> None:
    assert CacheBreakpoint("a") != CacheBreakpoint("b")


def test_empty_string_rejected() -> None:
    with pytest.raises(ValueError):
        CacheBreakpoint("")


def test_non_string_rejected() -> None:
    with pytest.raises(ValueError):
        CacheBreakpoint(123)  # type: ignore[arg-type]


def test_construction_has_no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing a CacheBreakpoint must not touch any LLM client/provider."""
    import llm_service.factory as factory_mod

    called = {"n": 0}

    def _boom(*_args: object, **_kwargs: object) -> None:
        called["n"] += 1
        raise AssertionError("CacheBreakpoint construction must not call get_client")

    monkeypatch.setattr(factory_mod, "get_client", _boom)
    CacheBreakpoint("prefix")
    assert called["n"] == 0


def test_public_export_from_package_root() -> None:
    import llm_service

    assert llm_service.CacheBreakpoint is CacheBreakpoint
