"""Tests for the cognition side channel (Step 7).

The channel no longer carries the tool audit: the shim drives the loop and reads
the audit off the runner's return value, so there is no ambient/importable audit
writer for agent code to forge into. These tests confirm the channel is reduced
to the read-only cognition side channel.
"""

from __future__ import annotations

import agent_cognition.tools as tools_pkg
import agent_cognition.tools.channel as ch


def test_no_audit_writer_or_sink_is_exposed() -> None:
    # The whole forgery surface is gone — no public *or* private audit writer.
    for name in (
        "record_tool_audit",
        "_record_brokered",
        "_recording_window",
        "collect_tool_audit",
    ):
        assert not hasattr(ch, name), name
        assert name not in ch.__all__
        assert name not in tools_pkg.__all__


def test_cognition_context_is_isolated_per_channel() -> None:
    assert ch.get_cognition_context() is None
    with ch.runtime_channel({"memory_digest": "d", "rules": []}):
        assert ch.get_cognition_context() == {"memory_digest": "d", "rules": []}
    assert ch.get_cognition_context() is None


def test_runtime_channel_resets_on_exception() -> None:
    try:
        with ch.runtime_channel({"x": 1}):
            assert ch.get_cognition_context() == {"x": 1}
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert ch.get_cognition_context() is None


def test_none_cognition_is_supported() -> None:
    with ch.runtime_channel(None):
        assert ch.get_cognition_context() is None
