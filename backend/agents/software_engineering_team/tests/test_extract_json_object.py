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


def test_schema_echo_before_repair_needing_verdict_selects_verdict() -> None:
    """A strict format echo must not win over a later, repair-needing real payload:
    position, not strictness, is the authority signal once a candidate is accepted."""
    text = (
        'Format: {"approved": true, "issues": []}\n'
        'Verdict: {"approved": false, "issues": ["missing tests"],}'  # trailing comma → repair
    )
    assert extract_json_object(text, required_keys=("approved",)) == {
        "approved": False,
        "issues": ["missing tests"],
    }


def test_required_keys_filters_trailing_usage_echo() -> None:
    """A payload followed by a usage/telemetry echo that lacks the anchor key must
    not be shadowed by the trailing object."""
    text = 'verdict: {"approved": false, "issues": ["x"]}\nUsage: {"tokens": 123}'
    assert extract_json_object(text, required_keys=("approved",)) == {
        "approved": False,
        "issues": ["x"],
    }
    # Without the anchor, the trailing object wins (no schema to discriminate) —
    # documents why callers with a known schema must pass required_keys.
    assert extract_json_object(text) == {"tokens": 123}


def test_empty_dict_does_not_beat_repaired_payload() -> None:
    """A trailing strict ``{}`` must not outrank an earlier repaired non-empty
    object — non-empty is the primary rank key."""
    assert extract_json_object('{"a": 1,} and then {}') == {"a": 1}


def test_envelope_wrapped_payload_is_recovered() -> None:
    """A payload nested one level inside a rejected envelope is recovered via descent."""
    text = '{"result": {"tasks": [{"id": "t1"}], "note": "wrapped"}}'
    assert extract_json_object(text, required_keys=("tasks",)) == {
        "tasks": [{"id": "t1"}],
        "note": "wrapped",
    }


def test_fenced_draft_inside_think_block_is_not_resurrected() -> None:
    """The fence fallback searches the wrapper-stripped text, so a fenced draft
    inside a removed <think> block is not mistaken for the answer."""
    text = (
        '<think>draft:\n```json\n{"approved": true}\n```\n</think>\nI cannot complete this review.'
    )
    assert extract_json_object(text, required_keys=("approved",)) is None


def test_real_object_under_unclosed_prose_brace_is_recovered() -> None:
    """A never-closed prose brace before the payload used to swallow it; the strict
    recall scan now finds the real object regardless."""
    text = 'the set {1, 2 and more items ... verdict: {"approved": true}'
    assert extract_json_object(text, required_keys=("approved",)) == {"approved": True}


def test_prose_quote_containing_brace_does_not_corrupt_scan() -> None:
    """A prose quotation mark that contains a '{' must not derail recovery of a
    later real object."""
    text = 'He said "an open { brace" and then: {"a": 1}'
    assert extract_json_object(text) == {"a": 1}


def test_truncated_dangling_key_is_not_fabricated_into_prose_payload() -> None:
    """A balanced prose fragment must never be repaired into a fabricated dict."""
    assert extract_json_object('{see the "spec": section above}') is None


def test_leading_payload_survives_many_trailing_junk_objects() -> None:
    """A long tail of anchor-less junk objects must not starve the real payload."""
    text = '{"approved": true} ' + " ".join('{"evt": %d}' % i for i in range(80))
    assert extract_json_object(text, required_keys=("approved",)) == {"approved": True}


def test_fenced_payload_under_unclosed_prose_brace_is_repaired() -> None:
    """The fence fallback recovers a fenced object that the span scan can't reach
    because an earlier unclosed prose brace nests it — and repairs it (trailing
    comma) via the fence path, not the span path."""
    text = 'note: config { started but never closed\n```json\n{"approved": true,}\n```\n'
    assert extract_json_object(text, required_keys=("approved",)) == {"approved": True}
