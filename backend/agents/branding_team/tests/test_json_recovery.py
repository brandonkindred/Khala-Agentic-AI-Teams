"""Tests for branding_team.shared.json_recovery.recover_json_object."""

from __future__ import annotations

from branding_team.shared.json_recovery import recover_json_object


def test_clean_json_is_parsed() -> None:
    assert recover_json_object('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_fenced_json_is_parsed() -> None:
    assert recover_json_object('```json\n{"n": 5}\n```') == {"n": 5}


def test_prose_wrapped_json_is_parsed() -> None:
    assert recover_json_object('Here you go: {"a": 1} thanks') == {"a": 1}


def test_unparseable_text_returns_none() -> None:
    assert recover_json_object("not json at all") is None


def test_empty_string_returns_none() -> None:
    assert recover_json_object("") is None


def test_whitespace_only_returns_none() -> None:
    assert recover_json_object("   \n\t  ") is None


def test_truncated_object_is_not_repaired() -> None:
    """Strict mode (repair=False) must not fabricate a closing brace."""
    assert recover_json_object('{"tasks": [{"id": "t1"') is None


def test_trailing_comma_is_not_repaired() -> None:
    """Strict mode (repair=False) must not tolerate a trailing comma."""
    assert recover_json_object('{"a": 1,}') is None
