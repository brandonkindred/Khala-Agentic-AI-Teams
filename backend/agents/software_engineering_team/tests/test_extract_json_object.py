"""Tests for shared_llm_recovery.recovery.extract_json_object (generic salvage)."""

from __future__ import annotations

from shared_llm_recovery import extract_json_object


def test_empty_or_blank_returns_none() -> None:
    assert extract_json_object("") is None
    assert extract_json_object("   ") is None


def test_no_json_returns_none() -> None:
    assert extract_json_object("no json here at all") is None


def test_prose_wrapped_object() -> None:
    assert extract_json_object('Here you go: {"a": 1, "b": [2, 3]} thanks') == {
        "a": 1,
        "b": [2, 3],
    }


def test_strips_think_block() -> None:
    assert extract_json_object('<think>let me reason</think>\n{"ok": true}') == {"ok": True}


def test_unwraps_json_tag() -> None:
    assert extract_json_object('<json>{"x": 42}</json>') == {"x": 42}


def test_fenced_block_fallback() -> None:
    assert extract_json_object('prose\n```json\n{"n": 5}\n```\n') == {"n": 5}


def test_skips_invalid_first_object_then_finds_valid() -> None:
    assert extract_json_object('{not json} then {"good": 1}') == {"good": 1}


def test_nested_braces() -> None:
    assert extract_json_object('noise {"outer": {"inner": 1}} noise') == {"outer": {"inner": 1}}


def test_top_level_array_is_not_an_object() -> None:
    assert extract_json_object("[1, 2, 3]") is None
