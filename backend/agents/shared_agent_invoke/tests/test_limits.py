"""Tests for the invoke size limits — esp. the tool-audit cap (Step 7)."""

from __future__ import annotations

import json

import pytest

from shared_agent_invoke.limits import (
    DEFAULT_MAX_WRITEBACK_BYTES,
    cap_tool_audit,
    max_writeback_bytes,
)


def test_audit_under_cap_is_unchanged() -> None:
    entries = [{"tool_id": "git", "ok": True}, {"tool_id": "http", "ok": False}]
    capped, truncated = cap_tool_audit(entries, max_bytes=10_000)
    assert truncated is False
    assert capped == entries


def test_audit_over_cap_keeps_leading_entries_and_marks_truncation() -> None:
    entries = [{"tool_id": f"t{i}", "blob": "x" * 100} for i in range(50)]
    capped, truncated = cap_tool_audit(entries, max_bytes=500)
    assert truncated is True
    # Serialised result respects the cap.
    assert len(json.dumps(capped)) <= 500
    # Leading entries are preserved (oldest-first), trailer is a truncation marker.
    assert capped[0]["tool_id"] == "t0"
    marker = capped[-1]
    assert marker["__truncated__"] is True
    assert marker["original_count"] == 50
    assert marker["dropped"] >= 1


def test_audit_first_entry_too_big_still_bounds() -> None:
    entries = [{"tool_id": "huge", "blob": "x" * 10_000}]
    capped, truncated = cap_tool_audit(entries, max_bytes=200)
    assert truncated is True
    assert capped == [{"__truncated__": True, "dropped": 1, "original_count": 1}]


def test_empty_audit_is_unchanged() -> None:
    capped, truncated = cap_tool_audit([], max_bytes=100)
    assert capped == []
    assert truncated is False


def test_max_writeback_bytes_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_COGNITION_WRITEBACK_MAX_BYTES", raising=False)
    assert max_writeback_bytes() == DEFAULT_MAX_WRITEBACK_BYTES
    monkeypatch.setenv("AGENT_COGNITION_WRITEBACK_MAX_BYTES", "2048")
    assert max_writeback_bytes() == 2048
