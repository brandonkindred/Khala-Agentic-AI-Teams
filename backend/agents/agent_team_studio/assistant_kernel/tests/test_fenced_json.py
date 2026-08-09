"""Unit tests for :mod:`agent_team_studio.assistant_kernel.fenced_json`."""

from __future__ import annotations

import pytest

from agent_team_studio.assistant_kernel.fenced_json import (
    merge_list_by_key,
    parse_fenced_json,
    strip_fenced_blocks,
)

_REPLY = """\
Here's a draft for you.

```agent
{"name": "blogging.planner", "role": "Plans outlines"}
```

```suggestions
["Add a word_count input?", "Target an industry?"]
```
"""


# ---------------------------------------------------------------------------
# parse_fenced_json
# ---------------------------------------------------------------------------


def test_parse_extracts_object_block() -> None:
    block = parse_fenced_json(_REPLY, "agent")
    assert block == {"name": "blogging.planner", "role": "Plans outlines"}


def test_parse_extracts_array_block() -> None:
    block = parse_fenced_json(_REPLY, "suggestions", expected_type=list)
    assert block == ["Add a word_count input?", "Target an industry?"]


def test_parse_none_when_tag_absent() -> None:
    assert parse_fenced_json("just prose, no block", "agent") is None


def test_parse_none_on_malformed_json() -> None:
    assert parse_fenced_json("```agent\n{not json}\n```", "agent") is None


def test_parse_none_when_wrong_top_level_type() -> None:
    # Default expected_type=dict rejects a top-level array.
    assert parse_fenced_json('```agent\n["a", "b"]\n```', "agent") is None


def test_parse_none_when_array_expected_but_object_found() -> None:
    assert (
        parse_fenced_json('```suggestions\n{"a": 1}\n```', "suggestions", expected_type=list)
        is None
    )


def test_parse_takes_first_of_multiple_blocks() -> None:
    text = '```agent\n{"name": "first"}\n```\n\n```agent\n{"name": "second"}\n```'
    assert parse_fenced_json(text, "agent") == {"name": "first"}


def test_parse_distinguishes_similarly_named_tags() -> None:
    # "agents" (plural) must not be matched when parsing "agent".
    text = '```agents\n[{"agent_name": "A"}]\n```'
    assert parse_fenced_json(text, "agent") is None
    assert parse_fenced_json(text, "agents", expected_type=list) == [{"agent_name": "A"}]


def test_parse_distinguishes_punctuation_suffixed_tags() -> None:
    # "agent-v2" extends "agent" with punctuation, not a word character —
    # still a different tag, not a prefix match.
    text = '```agent-v2\n{"x": 1}\n```'
    assert parse_fenced_json(text, "agent") is None
    assert parse_fenced_json(text, "agent-v2") == {"x": 1}


def test_parse_handles_immediately_closed_empty_block() -> None:
    # The tag-boundary check also accepts the fence's closing backtick
    # directly after the tag (an empty body, itself invalid JSON -> None).
    assert parse_fenced_json("```agent```", "agent") is None


def test_parse_none_on_oversized_integer() -> None:
    # json.loads raises a plain ValueError (not JSONDecodeError) once an
    # integer literal exceeds Python's int-string conversion limit.
    text = "```agent\n" + "1" * 5000 + "\n```"
    assert parse_fenced_json(text, "agent") is None


def test_parse_none_on_excessive_nesting() -> None:
    # Deeply nested JSON can blow the interpreter's recursion limit inside
    # json.loads, raising RecursionError rather than JSONDecodeError.
    body = "[" * 3000 + "]" * 3000
    text = f"```agent\n{body}\n```"
    assert parse_fenced_json(text, "agent", expected_type=list) is None


# ---------------------------------------------------------------------------
# strip_fenced_blocks
# ---------------------------------------------------------------------------


def test_strip_removes_all_listed_tags() -> None:
    stripped = strip_fenced_blocks(_REPLY, ["agent", "suggestions"])
    assert "```" not in stripped
    assert stripped == "Here's a draft for you."


def test_strip_leaves_untagged_prose_untouched() -> None:
    assert strip_fenced_blocks("no blocks here", ["agent"]) == "no blocks here"


def test_strip_ignores_tags_not_present() -> None:
    stripped = strip_fenced_blocks("```agent\n{}\n```", ["agent", "process", "suggestions"])
    assert stripped == ""


def test_strip_does_not_consume_a_longer_tags_block() -> None:
    # Stripping "agent" must not also remove an unlisted ```agents``` block —
    # "agent" is a prefix of "agents", not the same tag.
    text = 'keep this\n\n```agents\n[{"agent_name": "A"}]\n```'
    assert strip_fenced_blocks(text, ["agent"]) == text


def test_strip_does_not_consume_a_punctuation_suffixed_tags_block() -> None:
    text = 'keep this\n\n```agent-v2\n{"x": 1}\n```'
    assert strip_fenced_blocks(text, ["agent"]) == text


# ---------------------------------------------------------------------------
# merge_list_by_key
# ---------------------------------------------------------------------------


def test_merge_overlays_matching_keys() -> None:
    current = [{"key": "a", "v": 1}, {"key": "b", "v": 2}]
    incoming = [{"key": "b", "v": 20}]
    merged = merge_list_by_key(current, incoming, key="key")
    assert merged == [{"key": "a", "v": 1}, {"key": "b", "v": 20}]


def test_merge_preserves_entries_incoming_omits() -> None:
    current = [{"key": "a", "v": 1}, {"key": "b", "v": 2}, {"key": "c", "v": 3}]
    incoming = [{"key": "b", "v": 99}]
    merged = merge_list_by_key(current, incoming, key="key")
    assert [e["key"] for e in merged] == ["a", "b", "c"]
    assert merged[1] == {"key": "b", "v": 99}


def test_merge_appends_new_keys_in_incoming_order() -> None:
    current = [{"key": "a", "v": 1}]
    incoming = [{"key": "c", "v": 3}, {"key": "b", "v": 2}]
    merged = merge_list_by_key(current, incoming, key="key")
    assert [e["key"] for e in merged] == ["a", "c", "b"]


def test_merge_empty_incoming_returns_current_unchanged_content() -> None:
    current = [{"key": "a", "v": 1}]
    assert merge_list_by_key(current, [], key="key") == current


def test_merge_does_not_mutate_inputs() -> None:
    current = [{"key": "a", "v": 1}]
    incoming = [{"key": "a", "v": 2}]
    merge_list_by_key(current, incoming, key="key")
    assert current == [{"key": "a", "v": 1}]
    assert incoming == [{"key": "a", "v": 2}]


def test_merge_asserts_on_entry_missing_key() -> None:
    with pytest.raises(AssertionError):
        merge_list_by_key([{"key": "a"}], [{"not_key": "b"}], key="key")
