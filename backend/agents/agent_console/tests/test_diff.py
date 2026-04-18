"""Unit tests for the JSON unified-diff helper."""

from __future__ import annotations

from agent_console.diff import unified_json_diff


def test_identical_payloads_report_identical_and_empty_diff() -> None:
    text, identical = unified_json_diff({"a": 1}, {"a": 1})
    assert identical is True
    assert text == ""


def test_simple_change_produces_plus_and_minus_lines() -> None:
    text, identical = unified_json_diff(
        {"a": 1, "b": 2},
        {"a": 1, "b": 3},
        left_label="before",
        right_label="after",
    )
    assert identical is False
    assert "before" in text
    assert "after" in text
    assert any(line.startswith("-") and "2" in line for line in text.splitlines())
    assert any(line.startswith("+") and "3" in line for line in text.splitlines())


def test_diff_is_deterministic_across_key_order() -> None:
    # Keys intentionally inserted in opposite orders. Sorted pretty-print
    # makes the diff ignore dict ordering.
    text1, id1 = unified_json_diff({"a": 1, "b": 2}, {"b": 2, "a": 1})
    text2, id2 = unified_json_diff({"b": 2, "a": 1}, {"a": 1, "b": 2})
    assert id1 is True
    assert id2 is True
    assert text1 == ""
    assert text2 == ""


def test_labels_are_threaded_through() -> None:
    text, _ = unified_json_diff(
        [1, 2, 3],
        [1, 2, 4],
        left_label="left:foo",
        right_label="right:bar",
    )
    assert "left:foo" in text
    assert "right:bar" in text
