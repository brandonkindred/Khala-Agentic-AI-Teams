"""Tests for the runtime channels — esp. that the trusted audit can only be
written through the broker path (Step 7).
"""

from __future__ import annotations

import agent_cognition.tools as tools_pkg
import agent_cognition.tools.channel as ch


def test_no_public_audit_writer_is_exported() -> None:
    # The forgery vector — a public record_tool_audit — must not exist.
    assert "record_tool_audit" not in tools_pkg.__all__
    assert not hasattr(ch, "record_tool_audit")
    assert "record_tool_audit" not in ch.__all__


def test_record_is_a_noop_outside_a_recording_window() -> None:
    with ch.collect_tool_audit() as sink:
        # Sink is open, but no broker recording window is active.
        assert ch._record_brokered({"tool_id": "forged"}) is False
    assert sink == []


def test_record_inside_window_appends() -> None:
    with ch.collect_tool_audit() as sink:
        with ch._recording_window():
            assert ch._record_brokered({"tool_id": "real", "ok": True}) is True
    assert sink == [{"tool_id": "real", "ok": True}]


def test_record_inside_window_without_sink_is_noop() -> None:
    # A recording window with no open sink (no channel) must not raise.
    with ch._recording_window():
        assert ch._record_brokered({"tool_id": "x"}) is False


def test_recording_window_resets_on_exit() -> None:
    with ch.collect_tool_audit() as sink:
        with ch._recording_window():
            ch._record_brokered({"a": 1})
        # Window closed again — further writes are refused.
        assert ch._record_brokered({"a": 2}) is False
    assert sink == [{"a": 1}]


def test_cognition_context_isolated_per_channel() -> None:
    assert ch.get_cognition_context() is None
    with ch.runtime_channel({"memory_digest": "d"}, None):
        assert ch.get_cognition_context() == {"memory_digest": "d"}
    assert ch.get_cognition_context() is None
