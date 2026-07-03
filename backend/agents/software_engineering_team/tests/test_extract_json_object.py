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


def test_schema_echo_before_real_payload_is_not_selected() -> None:
    """A format example echoed before the verdict must not shadow the verdict."""
    text = (
        'I will answer in the format {"approved": true, "reason": "..."}.\n'
        'My verdict: {"approved": false, "issues": ["missing tests"]}'
    )
    assert extract_json_object(text) == {"approved": False, "issues": ["missing tests"]}


def test_empty_dict_in_prose_does_not_shadow_payload() -> None:
    text = 'Note: default schema is {} unless specified.\n{"approved": false}'
    assert extract_json_object(text) == {"approved": False}


def test_braces_inside_string_values_do_not_break_scan() -> None:
    text = 'ok {"reason": "use } to close the block", "approved": false}'
    assert extract_json_object(text) == {"reason": "use } to close the block", "approved": False}


def test_trailing_comma_is_repaired() -> None:
    assert extract_json_object('{"tasks": [{"id": "t1"}],}') == {"tasks": [{"id": "t1"}]}


def test_truncated_object_is_repaired() -> None:
    out = extract_json_object('Here is the plan: {"tasks": [{"id": "t1"}, {"id": "t2"')
    assert isinstance(out, dict)
    assert out.get("tasks"), "truncated task list should be completed by repair"


def test_deep_nesting_does_not_raise() -> None:
    text = '{"a":' * 2000 + "1" + "}" * 2000
    # Postcondition is "never raises"; the value may be None or a dict.
    extract_json_object(text)


def test_prose_braces_do_not_fabricate_a_payload() -> None:
    assert extract_json_object("{not json}") is None


def test_large_unbalanced_input_completes_quickly() -> None:
    import time

    text = "x" + "{" * 20_000
    start = time.monotonic()
    extract_json_object(text)
    assert time.monotonic() - start < 2.0, "salvage scan must stay linear-time"
