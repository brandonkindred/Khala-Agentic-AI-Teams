"""Targeted tests for the shared ``extract_json_object`` helper.

The three spec-authoring agents (``DesignAgent``, ``DesignReviewAgent``,
``CodeSynthesisAgent``) all delegate JSON parsing to
:func:`extract_json_object`; these tests pin the corner-cases (markdown
fence stripping, brace balancing, parse-error mapping).
"""

from __future__ import annotations

import json

import pytest

from investment_team.strategy_lab.agents._parse_helpers import extract_json_object


def test_extracts_plain_json_object() -> None:
    out = extract_json_object('{"ready": true, "issues": []}')
    assert out == {"ready": True, "issues": []}


def test_strips_markdown_fence() -> None:
    out = extract_json_object('```json\n{"ready": true}\n```')
    assert out == {"ready": True}


def test_strips_unlabelled_fence() -> None:
    out = extract_json_object('```\n{"ok": 1}\n```')
    assert out == {"ok": 1}


def test_returns_outermost_object_when_text_surrounds() -> None:
    out = extract_json_object('preamble {"a": 1, "b": {"c": 2}} trailer')
    assert out == {"a": 1, "b": {"c": 2}}


def test_raises_value_error_when_no_object_present() -> None:
    with pytest.raises(ValueError):
        extract_json_object("just prose, no JSON at all")


def test_raises_value_error_on_invalid_json_substring() -> None:
    """A ``{ ... }`` substring that isn't valid JSON surfaces as ValueError."""
    with pytest.raises(ValueError):
        extract_json_object('{"ready": this-is-not-valid}')


# ---------------------------------------------------------------------------
# String-aware brace scanning: braces inside JSON string values (e.g. a full
# Python program in ``strategy_code``) must not balance the scan early.
# ---------------------------------------------------------------------------


def test_unbalanced_close_brace_inside_string_value() -> None:
    """A lone '}' inside a string value must not truncate the object."""
    obj = {"strategy_code": 'reason = "close }"\nd = compute()', "changes_made": "x"}
    raw = json.dumps(obj)
    assert extract_json_object(raw) == obj


def test_unbalanced_open_brace_inside_string_value() -> None:
    """A lone '{' inside a string value must not leave the scan unbalanced."""
    obj = {"code": 'fmt = "{ not json"  # dangling', "note": "ok"}
    raw = json.dumps(obj)
    assert extract_json_object(raw) == obj


def test_balanced_braces_inside_string_value() -> None:
    """Dict/f-string braces inside a string value round-trip cleanly."""
    obj = {"code": 'd = {1: 2}\nf"{x:>{w}}"', "note": "ok"}
    raw = json.dumps(obj)
    assert extract_json_object(raw) == obj


def test_escaped_quote_then_brace_inside_string_value() -> None:
    """An escaped quote must not close the string early, so a following brace
    stays inside the string and is ignored by the depth scan."""
    obj = {"k": 'a " } b', "j": 1}
    raw = json.dumps(obj)  # -> {"k": "a \" } b", "j": 1}
    assert extract_json_object(raw) == obj


def test_string_with_braces_then_trailing_prose() -> None:
    """Surrounding prose + a brace-laden string value: outermost object wins."""
    obj = {"strategy_code": "x = {}  # } trailing brace", "changes_made": "y"}
    raw = "Here is the fix: " + json.dumps(obj) + " -- done"
    assert extract_json_object(raw) == obj
