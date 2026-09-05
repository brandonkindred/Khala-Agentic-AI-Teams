"""Tests for the shared text-parsing helpers in ``shared/text_parsing.py``."""

from __future__ import annotations

from typing import Any

import pytest
from agents.blogging.shared import text_parsing as tp
from strands.types.exceptions import EventLoopException

# ---------------------------------------------------------------------------
# unwrap_llm_cause
# ---------------------------------------------------------------------------


def test_unwrap_llm_cause_unwraps_event_loop_exception() -> None:
    original = ValueError("boom")
    wrapped = EventLoopException(original)
    assert tp.unwrap_llm_cause(wrapped) is original


def test_unwrap_llm_cause_passes_through_plain_exception() -> None:
    exc = RuntimeError("plain")
    assert tp.unwrap_llm_cause(exc) is exc


def test_unwrap_llm_cause_passes_through_when_original_exception_is_none() -> None:
    wrapped = EventLoopException(None)
    assert tp.unwrap_llm_cause(wrapped) is wrapped


def test_unwrap_llm_cause_only_unwraps_one_level_of_nested_event_loop_exception() -> None:
    """A nested EventLoopException chain unwraps exactly one level.

    ``unwrap_llm_cause`` returns ``original_exception`` verbatim without
    recursing, so an EventLoopException wrapping another EventLoopException
    unwraps to that inner EventLoopException itself, not further down to its
    own original_exception.
    """
    innermost = ValueError("root cause")
    inner_wrapper = EventLoopException(innermost)
    outer_wrapper = EventLoopException(inner_wrapper)

    result = tp.unwrap_llm_cause(outer_wrapper)

    assert result is inner_wrapper
    assert result is not innermost
    assert isinstance(result, EventLoopException)


# ---------------------------------------------------------------------------
# extract_draft_after_marker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"draft": 0}\n---DRAFT---\n# Title\n\nBody text.',
        '{"draft": 0}\n---DRAFT---# Title\n\nBody text.',
        '{"draft": 0}---DRAFT---\n# Title\n\nBody text.',
        '{"draft": 0}---DRAFT---# Title\n\nBody text.',
    ],
)
def test_extract_draft_after_marker_handles_all_marker_variants(raw: str) -> None:
    assert tp.extract_draft_after_marker(raw) == "# Title\n\nBody text."


def test_extract_draft_after_marker_falls_back_to_json_draft_key() -> None:
    raw = '{"draft": "# Fallback title\\n\\nFallback body."}'
    assert tp.extract_draft_after_marker(raw) == "# Fallback title\n\nFallback body."


def test_extract_draft_after_marker_falls_back_to_fenced_json_draft_key() -> None:
    raw = '```json\n{"draft": "# Fenced\\n\\nBody"}\n```'
    assert tp.extract_draft_after_marker(raw) == "# Fenced\n\nBody"


def test_extract_draft_after_marker_rejects_non_string_draft_sentinel() -> None:
    assert tp.extract_draft_after_marker('{"draft": 0}') == ""


def test_extract_draft_after_marker_rejects_empty_string_draft() -> None:
    assert tp.extract_draft_after_marker('{"draft": "   "}') == ""


def test_extract_draft_after_marker_returns_empty_on_unparseable_text() -> None:
    assert tp.extract_draft_after_marker("not json and no marker at all") == ""


@pytest.mark.parametrize("raw", [None, "", 123, ["not", "a", "string"]])
def test_extract_draft_after_marker_returns_empty_for_none_empty_or_non_string_input(
    raw: Any,
) -> None:
    assert tp.extract_draft_after_marker(raw) == ""


# ---------------------------------------------------------------------------
# extract_json_array_from_text
# ---------------------------------------------------------------------------


def test_extract_json_array_from_text_matches_issue_required_keys() -> None:
    text = '[{"issue": "too vague", "severity": "high"}]'
    result = tp.extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "too vague", "severity": "high"}]


def test_extract_json_array_from_text_matches_question_required_keys() -> None:
    text = '[{"question": "what is the source?"}]'
    result = tp.extract_json_array_from_text(text, required_keys=("question",))
    assert result == [{"question": "what is the source?"}]


def test_extract_json_array_from_text_skips_leading_prose() -> None:
    text = 'Here is the review:\n\n[{"issue": "unclear claim"}]\n\nThanks.'
    result = tp.extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "unclear claim"}]


def test_extract_json_array_from_text_resumes_past_nested_bracket_in_non_match() -> None:
    """A non-matching array containing a nested ``[`` doesn't get re-entered and salvaged.

    This is the fixed-vs-drifted behavior the issue exists to preserve:
    resuming the scan at the decoded value's end (not one char past the
    opening bracket) means a nested "[" inside an already-rejected candidate
    can't be re-parsed on its own as if it were a fresh top-level match.
    Rejecting to ``search_from = i + 1`` here would instead re-enter the
    nested decoy array and incorrectly return it in place of the real
    payload that follows.
    """
    text = '[[{"issue": "nested-decoy"}], "filler"] then later: [{"issue": "the real one"}]'
    result = tp.extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "the real one"}]


def test_extract_json_array_from_text_does_not_salvage_from_inside_decoded_value() -> None:
    """After a successful decode of a non-matching array, the scanner must not
    re-enter that already-decoded span looking for a nested match. The outer
    array's own top-level elements are a list and a string (neither a dict),
    so it correctly does not match required_keys — but its first element is
    itself an array containing a dict with a truthy "issue" key. A real match
    must not be salvaged from inside an already-rejected value.
    """
    text = '[[{"issue": "wrongly-salvaged", "fix": "z"}], "sibling"]'
    assert tp.extract_json_array_from_text(text, required_keys=("issue",)) is None


def test_extract_json_array_from_text_empty_array_fallback() -> None:
    text = "Some prose with an empty markdown link []() in it."
    result = tp.extract_json_array_from_text(text, required_keys=("issue",))
    assert result == []


def test_extract_json_array_from_text_returns_none_when_no_bracket_present() -> None:
    assert tp.extract_json_array_from_text("no brackets here", required_keys=("issue",)) is None


def test_extract_json_array_from_text_skips_schema_mismatched_array() -> None:
    text = '[1] then [{"issue": "real payload"}]'
    result = tp.extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "real payload"}]


def test_extract_json_array_from_text_skips_unparseable_bracket() -> None:
    text = 'malformed [not valid json then [{"issue": "real payload"}]'
    result = tp.extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "real payload"}]


def test_extract_json_array_from_text_accepts_array_when_any_element_matches() -> None:
    """An array matches (and is returned whole) if ANY element carries required_keys.

    Pins the "any", not "all", semantics: a real payload can contain
    individually malformed items alongside valid ones, and the whole array
    is still accepted and returned as-is (the caller's own per-item
    validation is expected to skip the non-matching elements).
    """
    text = '[{"issue": "ok"}, {"unrelated": 1}]'
    result = tp.extract_json_array_from_text(text, required_keys=("issue",))
    assert result == [{"issue": "ok"}, {"unrelated": 1}]


# ---------------------------------------------------------------------------
# looks_like_top_level_json_object
# ---------------------------------------------------------------------------


def test_looks_like_top_level_json_object_true_for_bare_object() -> None:
    assert tp.looks_like_top_level_json_object('{"a": 1}') is True


def test_looks_like_top_level_json_object_false_for_prose_wrapped_json() -> None:
    assert tp.looks_like_top_level_json_object('Here is the JSON: {"a": 1}') is False


def test_looks_like_top_level_json_object_false_for_fenced_json() -> None:
    assert tp.looks_like_top_level_json_object('```json\n{"a": 1}\n```') is False


def test_looks_like_top_level_json_object_false_for_non_object_json() -> None:
    assert tp.looks_like_top_level_json_object("[1, 2, 3]") is False


def test_looks_like_top_level_json_object_false_for_trailing_garbage() -> None:
    assert tp.looks_like_top_level_json_object('{"a": 1} trailing garbage') is False


def test_looks_like_top_level_json_object_true_with_surrounding_whitespace() -> None:
    assert tp.looks_like_top_level_json_object('  \n{"a": 1}\n  ') is True


def test_looks_like_top_level_json_object_false_for_malformed_json() -> None:
    assert tp.looks_like_top_level_json_object('{"a": 1') is False


# ---------------------------------------------------------------------------
# format_feedback_item_line
# ---------------------------------------------------------------------------


class _FeedbackItem:
    """Duck-typed stand-in for a feedback item; every field defaults to None."""

    def __init__(self, severity=None, category=None, issue=None, location=None, suggestion=None):
        self.severity = severity
        self.category = category
        self.issue = issue
        self.location = location
        self.suggestion = suggestion


def test_format_feedback_item_line_minimal() -> None:
    item = _FeedbackItem(severity="high", category="clarity", issue="Confusing paragraph")
    assert tp.format_feedback_item_line(item, 1) == "1. [high] clarity: Confusing paragraph"


def test_format_feedback_item_line_with_location() -> None:
    item = _FeedbackItem(
        severity="medium", category="tone", issue="Too casual", location="paragraph 2"
    )
    line = tp.format_feedback_item_line(item, 3)
    assert line == "3. [medium] tone [paragraph 2]: Too casual"


def test_format_feedback_item_line_with_suggestion() -> None:
    item = _FeedbackItem(
        severity="low", category="style", issue="Passive voice", suggestion="Use active voice"
    )
    line = tp.format_feedback_item_line(item, 2)
    assert line == "2. [low] style: Passive voice\n   Suggestion: Use active voice"


def test_format_feedback_item_line_with_location_and_suggestion() -> None:
    item = _FeedbackItem(
        severity="medium",
        category="tone",
        issue="Too casual",
        location="paragraph 2",
        suggestion="Use active voice",
    )
    line = tp.format_feedback_item_line(item, 3)
    assert line == "3. [medium] tone [paragraph 2]: Too casual\n   Suggestion: Use active voice"


def test_format_feedback_item_line_rejects_missing_required_field() -> None:
    item = _FeedbackItem(severity="high", category="clarity", issue=None)
    with pytest.raises(ValueError, match="missing required fields"):
        tp.format_feedback_item_line(item, 1)


@pytest.mark.parametrize("bad_index", [0, -1, 1.5, "1", True, False])
def test_format_feedback_item_line_rejects_non_positive_or_non_int_index(bad_index: Any) -> None:
    item = _FeedbackItem(severity="high", category="clarity", issue="Some issue")
    with pytest.raises(ValueError, match="positive int"):
        tp.format_feedback_item_line(item, bad_index)


# ---------------------------------------------------------------------------
# Public re-exports from shared/__init__.py
# ---------------------------------------------------------------------------


def test_helpers_are_reexported_from_shared_package() -> None:
    from agents.blogging import shared

    assert shared.unwrap_llm_cause is tp.unwrap_llm_cause
    assert shared.extract_draft_after_marker is tp.extract_draft_after_marker
    assert shared.extract_json_array_from_text is tp.extract_json_array_from_text
    assert shared.looks_like_top_level_json_object is tp.looks_like_top_level_json_object
    assert shared.format_feedback_item_line is tp.format_feedback_item_line
