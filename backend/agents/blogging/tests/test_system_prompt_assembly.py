"""Tests for the shared system prompt assembly helpers."""

from __future__ import annotations

from agents.blogging.shared.system_prompt_assembly import (
    build_blogging_system_prompt_content,
    build_system_prompt_with_content,
)

from llm_service import CacheBreakpoint


def test_both_texts_present_join_into_single_cache_breakpoint() -> None:
    result = build_blogging_system_prompt_content("brand spec text", "writing guideline text")

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], CacheBreakpoint)
    assert result[0].text == "brand spec text\n\nwriting guideline text"


def test_brand_spec_only_returns_single_cache_breakpoint() -> None:
    result = build_blogging_system_prompt_content("brand spec text", "")

    assert result == [CacheBreakpoint("brand spec text")]


def test_writing_guideline_only_returns_single_cache_breakpoint() -> None:
    result = build_blogging_system_prompt_content("", "writing guideline text")

    assert result == [CacheBreakpoint("writing guideline text")]


def test_both_empty_returns_none() -> None:
    assert build_blogging_system_prompt_content("", "") is None


def test_whitespace_only_texts_return_none() -> None:
    assert build_blogging_system_prompt_content("   \n", "\t  ") is None


def test_mixed_blank_and_whitespace_one_side() -> None:
    result = build_blogging_system_prompt_content("brand spec text", "   \n")

    assert result == [CacheBreakpoint("brand spec text")]


def test_with_content_returns_persona_unchanged_when_content_is_none() -> None:
    assert build_system_prompt_with_content("You are a helpful writer.", None) == (
        "You are a helpful writer."
    )


def test_with_content_returns_persona_unchanged_when_content_is_empty_list() -> None:
    assert build_system_prompt_with_content("You are a helpful writer.", []) == (
        "You are a helpful writer."
    )


def test_with_content_wraps_persona_and_passes_cache_breakpoint_through() -> None:
    segment = CacheBreakpoint("brand + style text")

    result = build_system_prompt_with_content("You are a helpful writer.", [segment])

    assert result == [{"text": "You are a helpful writer."}, segment]


def test_with_content_normalizes_bare_string_segments() -> None:
    result = build_system_prompt_with_content("Persona.", ["extra context"])

    assert result == [{"text": "Persona."}, {"text": "extra context"}]


def test_with_content_reexport_is_llm_service_implementation() -> None:
    """This module re-exports llm_service's implementation rather than owning
    a second copy — pin the identity so import-path drift is caught here."""
    import llm_service

    assert build_system_prompt_with_content is llm_service.build_system_prompt_with_content
