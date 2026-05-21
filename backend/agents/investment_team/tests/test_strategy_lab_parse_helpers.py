"""Targeted tests for the shared ``extract_json_object`` helper.

The three spec-authoring agents (``DesignAgent``, ``DesignReviewAgent``,
``CodeSynthesisAgent``) all delegate JSON parsing to
:func:`extract_json_object`; these tests pin the corner-cases (markdown
fence stripping, brace balancing, parse-error mapping).
"""

from __future__ import annotations

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
