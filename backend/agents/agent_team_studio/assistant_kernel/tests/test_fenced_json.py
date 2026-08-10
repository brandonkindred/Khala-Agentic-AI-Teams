"""Unit tests for :mod:`agent_team_studio.assistant_kernel.fenced_json`."""

from __future__ import annotations

import json

import pytest

from agent_team_studio.assistant_kernel import fenced_json as fenced_json_module
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


def test_parse_none_when_tag_immediately_closed_with_no_body() -> None:
    # A backtick is not accepted as a tag-boundary character (only
    # whitespace is), so a same-line "```agent```" fails the boundary check
    # and never matches — no fenced block, not just an empty/invalid one.
    assert parse_fenced_json("```agent```", "agent") is None


def test_parse_none_when_immediately_closed_tag_precedes_an_unrelated_block() -> None:
    # A self-closed ```agent``` marker earlier in the text must not be
    # read as the *opening* of a new block whose body then swallows
    # everything up to some later, unrelated block's closing fence.
    text = '```agent``` some prose\n\nkeep this paragraph\n\n```suggestions\n["a", "b"]\n```\n'
    assert parse_fenced_json(text, "agent") is None
    assert parse_fenced_json(text, "suggestions", expected_type=list) == ["a", "b"]


def test_parse_body_containing_embedded_backticks() -> None:
    # A JSON string value (e.g. a system_prompt) may legitimately contain a
    # literal ``` sequence, such as a markdown code example. The closing
    # fence detection must not stop at that embedded run — only a ``` that
    # starts its own line closes the block.
    inner = "Show an example like ```python\nprint(1)\n``` when relevant."
    payload = json.dumps({"name": "x", "system_prompt": inner})
    text = f"```agent\n{payload}\n```"
    assert parse_fenced_json(text, "agent") == {"name": "x", "system_prompt": inner}


def test_parse_tolerates_crlf_line_endings() -> None:
    text = '```agent\r\n{"name": "x"}\r\n```\r\n'
    assert parse_fenced_json(text, "agent") == {"name": "x"}


def test_parse_tolerates_trailing_whitespace_after_closing_fence() -> None:
    text = '```agent\n{"name": "x"}\n```   \n'
    assert parse_fenced_json(text, "agent") == {"name": "x"}


def test_parse_tolerates_indented_closing_fence() -> None:
    text = '```agent\n{"name": "x"}\n   ```\n'
    assert parse_fenced_json(text, "agent") == {"name": "x"}


def test_parse_none_on_oversized_integer() -> None:
    # json.loads raises a plain ValueError (not JSONDecodeError) once an
    # integer literal exceeds Python's int-string conversion limit.
    text = "```agent\n" + "1" * 5000 + "\n```"
    assert parse_fenced_json(text, "agent") is None


def test_parse_none_on_excessive_nesting(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deeply nested JSON can blow the interpreter's recursion limit inside
    # json.loads, raising RecursionError rather than JSONDecodeError. The
    # nesting depth needed to actually trigger that varies by Python
    # build/version — even lowering sys.setrecursionlimit() doesn't reliably
    # force it on every CPython build (the C-accelerated decoder doesn't
    # necessarily respect it the way pure-Python recursion does) — so this
    # simulates the failure directly by making json.loads raise, which
    # exercises the same except clause deterministically on every runtime.
    def _raise_recursion_error(*_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(fenced_json_module.json, "loads", _raise_recursion_error)
    text = "```agent\n[1, 2, 3]\n```"
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


def test_strip_does_not_swallow_an_unrelated_block_past_a_self_closed_marker() -> None:
    # Mirrors test_parse_none_when_immediately_closed_tag_precedes_an_unrelated_block:
    # a self-closed ```agent``` marker must not be treated as an opening
    # fence whose body extends through prose and an unrelated later block.
    text = '```agent``` some prose\n\nkeep this paragraph\n\n```suggestions\n["a", "b"]\n```'
    assert strip_fenced_blocks(text, ["agent"]) == text


def test_strip_removes_the_whole_block_despite_embedded_backticks() -> None:
    # Mirrors test_parse_body_containing_embedded_backticks: the embedded
    # ``` run must not truncate what gets stripped, leaking the remainder
    # of the JSON body into the visible reply.
    inner = "Show an example like ```python\nprint(1)\n``` when relevant."
    payload = json.dumps({"name": "x", "system_prompt": inner})
    text = f"prose before\n\n```agent\n{payload}\n```\n\nprose after"
    stripped = strip_fenced_blocks(text, ["agent"])
    assert stripped == "prose before\n\n\n\nprose after"
    assert "system_prompt" not in stripped
    assert "```" not in stripped


def test_strip_removes_adjacent_same_tag_blocks_with_no_prose_between() -> None:
    # Two "agent" blocks back-to-back with only whitespace between them: both
    # get matched and removed, and the leftover whitespace-only remainder is
    # stripped away, leaving an empty string (nothing to preserve).
    text = '```agent\n{"a": 1}\n```\n\n```agent\n{"b": 2}\n```'
    assert strip_fenced_blocks(text, ["agent"]) == ""


def test_strip_preserves_prose_between_same_tag_blocks() -> None:
    text = '```agent\n{"a": 1}\n```\n\nsome real prose\n\n```agent\n{"b": 2}\n```'
    assert strip_fenced_blocks(text, ["agent"]) == "some real prose"


def test_strip_preserves_trailing_prose_after_a_block_at_the_start() -> None:
    text = '```agent\n{"a": 1}\n```\n\nprose after'
    assert strip_fenced_blocks(text, ["agent"]) == "prose after"


def test_strip_preserves_leading_prose_before_a_block_at_the_end() -> None:
    text = 'prose before\n\n```agent\n{"a": 1}\n```'
    assert strip_fenced_blocks(text, ["agent"]) == "prose before"


def test_strip_removes_a_crlf_block_with_trailing_whitespace() -> None:
    text = 'prose before\r\n\r\n```agent\r\n{"a": 1}\r\n```   \r\n\r\nprose after'
    stripped = strip_fenced_blocks(text, ["agent"])
    assert "```" not in stripped
    assert '"a": 1' not in stripped
    assert stripped.startswith("prose before")
    assert stripped.endswith("prose after")


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


def test_merge_asserts_on_duplicate_key_in_current() -> None:
    with pytest.raises(AssertionError):
        merge_list_by_key([{"key": "a", "v": 1}, {"key": "a", "v": 2}], [], key="key")


def test_merge_last_wins_on_duplicate_key_within_incoming() -> None:
    # Duplicate keys are only a precondition violation for current;
    # incoming may legitimately repeat a key (e.g. a model echoing the same
    # entry twice), and the usual overlay last-wins semantics apply.
    current = [{"key": "a", "v": 1}]
    incoming = [{"key": "a", "v": 2}, {"key": "a", "v": 3}]
    assert merge_list_by_key(current, incoming, key="key") == [{"key": "a", "v": 3}]


def test_merge_is_shallow_entries_are_not_deep_copied() -> None:
    # The merge only guarantees the *lists* aren't mutated (see
    # test_merge_does_not_mutate_inputs) — entries themselves are shared
    # references, so mutating a merged entry is visible on its source input.
    current = [{"key": "a", "nested": {"count": 0}}]
    merged = merge_list_by_key(current, [], key="key")
    assert merged[0] is current[0]
    merged[0]["nested"]["count"] = 99
    assert current[0]["nested"]["count"] == 99
