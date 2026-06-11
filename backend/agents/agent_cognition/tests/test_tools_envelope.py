"""Tests for the marker-wrapped invoke envelope (Step 7).

Pure dict shaping — no Postgres, no LLM.
"""

from __future__ import annotations

import pytest

from agent_cognition.tools.envelope import (
    ENVELOPE_MARKER,
    WRITEBACK_MARKER,
    EnvelopeError,
    UnwrappedRequest,
    UnwrappedWriteback,
    is_envelope,
    try_unwrap_request,
    try_unwrap_writeback,
    wrap_request,
    wrap_writeback,
)


def test_wrap_round_trips_through_unwrap() -> None:
    wrapped = wrap_request({"q": "hi"}, {"rules": [], "memory_digest": "d"})
    assert wrapped[ENVELOPE_MARKER] == 1
    unwrapped = try_unwrap_request(wrapped)
    assert isinstance(unwrapped, UnwrappedRequest)
    assert unwrapped.input == {"q": "hi"}
    assert unwrapped.cognition == {"rules": [], "memory_digest": "d"}


def test_wrap_shallow_copies_cognition_and_references_input() -> None:
    cognition = {"rules": [1]}
    input_body = {"x": 1}
    wrapped = wrap_request(input_body, cognition)
    # Top-level is isolated (a fresh dict), so adding a key never leaks back.
    wrapped["cognition"]["memory_digest"] = "d"
    assert "memory_digest" not in cognition
    # input is referenced verbatim — the agent's body is forwarded unchanged.
    assert wrapped["input"] is input_body


def test_wrap_rejects_non_mapping_cognition() -> None:
    with pytest.raises(EnvelopeError):
        wrap_request({"x": 1}, ["not", "a", "mapping"])  # type: ignore[arg-type]


def test_non_object_input_is_preserved() -> None:
    # The agent's own input may be a scalar or list — the envelope carries it.
    for payload in (123, "text", [1, 2, 3], None):
        unwrapped = try_unwrap_request(wrap_request(payload, {}))
        assert unwrapped is not None
        assert unwrapped.input == payload


def test_unmarked_body_passes_through_as_none() -> None:
    # An agent whose *own* schema has a top-level `input` key (and no marker) must
    # not be mistaken for an envelope.
    assert try_unwrap_request({"input": {"real": "user data"}, "extra": 1}) is None
    assert try_unwrap_request({"q": "hi"}) is None
    assert try_unwrap_request("a string") is None
    assert try_unwrap_request([1, 2, 3]) is None


def test_is_envelope_is_presence_only() -> None:
    assert is_envelope({ENVELOPE_MARKER: 1, "input": {}, "cognition": {}}) is True
    assert is_envelope({ENVELOPE_MARKER: 1}) is True  # presence, not validity
    assert is_envelope({"input": {}}) is False
    assert is_envelope("nope") is False


def test_marked_but_missing_input_raises() -> None:
    with pytest.raises(EnvelopeError):
        try_unwrap_request({ENVELOPE_MARKER: 1, "cognition": {}})


def test_marked_but_non_object_cognition_raises() -> None:
    with pytest.raises(EnvelopeError):
        try_unwrap_request({ENVELOPE_MARKER: 1, "input": {}, "cognition": ["bad"]})


def test_marked_with_stray_keys_raises() -> None:
    with pytest.raises(EnvelopeError):
        try_unwrap_request({ENVELOPE_MARKER: 1, "input": {}, "cognition": {}, "smuggled": "x"})


def test_marked_but_missing_cognition_raises() -> None:
    # The contract is marker + input + cognition; a marked body without cognition
    # is malformed (rejected), not silently unwrapped with an empty block.
    with pytest.raises(EnvelopeError):
        try_unwrap_request({ENVELOPE_MARKER: 1, "input": {"a": 1}})


# --- response writeback envelope -------------------------------------------


def test_writeback_round_trips_through_unwrap() -> None:
    wb = {"events": [{"id": "e1"}], "tool_calls": [{"tool_id": "git"}], "truncated": False}
    wrapped = wrap_writeback({"output": "hi"}, wb)
    assert wrapped[WRITEBACK_MARKER] == 1
    unwrapped = try_unwrap_writeback(wrapped)
    assert isinstance(unwrapped, UnwrappedWriteback)
    assert unwrapped.output == {"output": "hi"}
    assert unwrapped.events == [{"id": "e1"}]
    # tool_calls are NOT exposed — the trusted tool audit is shim-owned.
    assert not hasattr(unwrapped, "tool_calls")


def test_wrap_writeback_shallow_copies_and_references_output() -> None:
    wb = {"events": []}
    output = {"output": "x"}
    wrapped = wrap_writeback(output, wb)
    # Top-level is a fresh dict, so adding a key never leaks back to the caller's.
    wrapped["writeback"]["truncated"] = True
    assert "truncated" not in wb
    # output is referenced verbatim.
    assert wrapped["output"] is output


def test_wrap_writeback_rejects_non_mapping() -> None:
    with pytest.raises(EnvelopeError):
        wrap_writeback({"output": "x"}, ["bad"])  # type: ignore[arg-type]


def test_unwrap_writeback_none_for_unmarked() -> None:
    assert try_unwrap_writeback({"output": "x"}) is None
    assert try_unwrap_writeback("plain") is None
    assert try_unwrap_writeback([1, 2]) is None


def test_unwrap_writeback_tolerates_malformed_writeback_half() -> None:
    # A usable output is never lost just because the writeback half is misshapen.
    unwrapped = try_unwrap_writeback({WRITEBACK_MARKER: 1, "output": "keep", "writeback": "bad"})
    assert unwrapped is not None
    assert unwrapped.output == "keep"
    assert unwrapped.events == []
    # Missing writeback key + non-list events also degrade to [].
    u2 = try_unwrap_writeback({WRITEBACK_MARKER: 1, "writeback": {"events": "x"}})
    assert u2.output is None
    assert u2.events == []
