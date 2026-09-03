"""Tests for the shared system-prompt assembly helper (``llm_service.system_prompt_assembly``)."""

from __future__ import annotations

import pytest

from llm_service.cache_breakpoint import CacheBreakpoint
from llm_service.system_prompt_assembly import build_system_prompt_with_content


def test_rejects_empty_persona_with_content() -> None:
    with pytest.raises(ValueError, match="system_prompt"):
        build_system_prompt_with_content("", [CacheBreakpoint("cached prefix")])


def test_rejects_empty_persona_without_content() -> None:
    with pytest.raises(ValueError, match="system_prompt"):
        build_system_prompt_with_content("", None)


def test_returns_str_when_no_content() -> None:
    result = build_system_prompt_with_content("persona text", None)
    assert result == "persona text"


def test_returns_str_when_empty_list() -> None:
    result = build_system_prompt_with_content("persona text", [])
    assert result == "persona text"


def test_returns_list_with_cache_breakpoint() -> None:
    bp = CacheBreakpoint("cached prefix")
    result = build_system_prompt_with_content("persona", [bp])
    assert isinstance(result, list)
    assert result[0] == {"text": "persona"}
    assert result[1] is bp


def test_normalizes_bare_strings() -> None:
    result = build_system_prompt_with_content("persona", ["extra context"])
    assert isinstance(result, list)
    assert result[0] == {"text": "persona"}
    assert result[1] == {"text": "extra context"}


def test_passes_dict_blocks_through_unchanged() -> None:
    block = {"text": "already a block", "cache_control": {"type": "ephemeral"}}
    result = build_system_prompt_with_content("persona", [block])
    assert result[1] is block


def test_mixed_segments_preserve_order() -> None:
    bp = CacheBreakpoint("cached")
    result = build_system_prompt_with_content("persona", ["plain string", bp])
    assert result == [{"text": "persona"}, {"text": "plain string"}, bp]


def test_public_export_from_package_root() -> None:
    import llm_service

    assert llm_service.build_system_prompt_with_content is build_system_prompt_with_content
