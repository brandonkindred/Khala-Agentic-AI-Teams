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


def test_contains_only_text_key() -> None:
    """Duck-types as a single-key ``{"text": ...}`` mapping -- required so a
    ``CacheBreakpoint`` placed in a Strands ``Agent``'s ``system_prompt=``
    list survives ``strands.types.content.split_system_prompt``'s
    ``"text" in block`` check (called both at ``Agent`` construction and
    again by the event loop on every turn)."""
    bp = CacheBreakpoint("prefix")
    assert "text" in bp
    assert "other_key" not in bp
    assert 0 not in bp


def test_getitem_returns_text_for_text_key() -> None:
    bp = CacheBreakpoint("prefix")
    assert bp["text"] == "prefix"


def test_getitem_raises_key_error_for_other_keys() -> None:
    bp = CacheBreakpoint("prefix")
    with pytest.raises(KeyError):
        bp["other_key"]


def test_dict_like_protocol_matches_native_text_block_shape() -> None:
    """A CacheBreakpoint and a native ``{"text": ...}`` block behave
    identically under the exact duck-typing Strands' internals use."""
    bp = CacheBreakpoint("prefix")
    native_block = {"text": "prefix"}
    assert ("text" in bp) == ("text" in native_block)
    assert bp["text"] == native_block["text"]


def test_isinstance_checks_unaffected_by_dict_like_protocol() -> None:
    """Adding __contains__/__getitem__ must not make a CacheBreakpoint
    register as a dict -- downstream consumers (strands_adapter,
    clients.claude) branch on isinstance(x, CacheBreakpoint) /
    isinstance(x, dict) and must keep telling the two apart."""
    bp = CacheBreakpoint("prefix")
    assert isinstance(bp, CacheBreakpoint)
    assert not isinstance(bp, dict)
