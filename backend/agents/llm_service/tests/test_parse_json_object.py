"""Tests for llm_service.util.parse_json_object, the canonical dict-returning
JSON-parse entrypoint every SE-team wrapper delegates to.

Recovery-ladder edge cases (fence stripping, prose-prefix stripping, trailing
comma repair, truncation repair, envelope descent, format-echo
disambiguation) are exhaustively characterized against ``extract_json_from_response``
in ``test_extract_json_from_response.py`` -- this module does not duplicate
that corpus. It instead verifies parse_json_object's own added behavior: the
``on_failure`` policy mapping applied on top of that shared recovery ladder.
"""

import pytest

from llm_service.interface import LLMJsonParseError
from llm_service.util import parse_json_object

FENCED_JSON = '```json\n{"ok": true}\n```'
PROSE_PREFIXED = 'Here is the JSON: {"ok": true}'
TRAILING_COMMA = '{"tasks": [{"id": "t1"}],}'
TRUNCATED_JSON = 'Here is the plan: {"tasks": [{"id": "t1"}, {"id": "t2"'
ENVELOPE_WRAPPED = (
    'Note: format looks like {"format": {"a": 1}} but the real answer is '
    '{"result": {"tasks": [{"id": "t1"}]}}'
)
UNRECOVERABLE = "I cannot complete this request because the file was not found."
BARE_ARRAY = "[1, 2, 3]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (FENCED_JSON, {"ok": True}),
        (PROSE_PREFIXED, {"ok": True}),
        (TRAILING_COMMA, {"tasks": [{"id": "t1"}]}),
        (TRUNCATED_JSON, {"tasks": [{"id": "t1"}, {"id": "t2"}]}),
    ],
)
def test_recovers_via_shared_ladder(text: str, expected: dict) -> None:
    assert parse_json_object(text) == expected


def test_expected_keys_forwarded_for_envelope_descent() -> None:
    result = parse_json_object(ENVELOPE_WRAPPED, expected_keys=frozenset({"tasks"}))
    assert result == {"tasks": [{"id": "t1"}]}


def test_on_failure_raise_propagates_on_unrecoverable_input() -> None:
    with pytest.raises(LLMJsonParseError):
        parse_json_object(UNRECOVERABLE, on_failure="raise")


def test_on_failure_none_returns_none_on_unrecoverable_input() -> None:
    assert parse_json_object(UNRECOVERABLE, on_failure="none") is None


def test_on_failure_empty_returns_empty_dict_on_unrecoverable_input() -> None:
    assert parse_json_object(UNRECOVERABLE, on_failure="empty") == {}


def test_on_failure_raise_passes_through_non_dict_result() -> None:
    assert parse_json_object(BARE_ARRAY, on_failure="raise") == [1, 2, 3]


def test_on_failure_none_maps_non_dict_result_to_none() -> None:
    assert parse_json_object(BARE_ARRAY, on_failure="none") is None


def test_on_failure_empty_maps_non_dict_result_to_empty_dict() -> None:
    assert parse_json_object(BARE_ARRAY, on_failure="empty") == {}


@pytest.mark.parametrize("on_failure", ["raise", "none", "empty"])
def test_empty_string_input(on_failure: str) -> None:
    if on_failure == "raise":
        with pytest.raises(LLMJsonParseError):
            parse_json_object("", on_failure=on_failure)
    else:
        expected = None if on_failure == "none" else {}
        assert parse_json_object("", on_failure=on_failure) == expected


def test_invalid_on_failure_policy_raises_assertion_error() -> None:
    with pytest.raises(AssertionError):
        parse_json_object('{"ok": true}', on_failure="bogus")


def test_default_on_failure_is_raise() -> None:
    with pytest.raises(LLMJsonParseError):
        parse_json_object(UNRECOVERABLE)
