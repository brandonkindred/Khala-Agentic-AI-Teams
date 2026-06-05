"""Unit tests for the shim's cognition bridge, incl. the no-cognition fallback.

These exercise ``shared_agent_invoke.cognition_envelope`` directly (no FastAPI),
including the degraded path where the ``agent_cognition`` package is absent.
"""

from __future__ import annotations

import sys

import pytest

from agent_cognition.tools.envelope import ENVELOPE_MARKER
from shared_agent_invoke.cognition_envelope import (
    CognitionEnvelopeError,
    open_cognition_runtime,
    unwrap_cognition_request,
)


def test_unwrap_returns_input_and_cognition() -> None:
    agent_input, cognition = unwrap_cognition_request(
        {ENVELOPE_MARKER: 1, "input": {"a": 1}, "cognition": {"memory_digest": "d"}}
    )
    assert agent_input == {"a": 1}
    assert cognition == {"memory_digest": "d"}


def test_unwrap_passes_unmarked_body_through() -> None:
    agent_input, cognition = unwrap_cognition_request({"plain": "body"})
    assert agent_input == {"plain": "body"}
    assert cognition is None


def test_unwrap_raises_on_malformed_envelope() -> None:
    with pytest.raises(CognitionEnvelopeError):
        unwrap_cognition_request({ENVELOPE_MARKER: 1})  # missing 'input'


@pytest.fixture()
def _no_cognition(monkeypatch: pytest.MonkeyPatch):
    """Make ``import agent_cognition.tools.{envelope,channel}`` fail."""
    monkeypatch.setitem(sys.modules, "agent_cognition.tools.envelope", None)
    monkeypatch.setitem(sys.modules, "agent_cognition.tools.channel", None)
    yield


def test_unwrap_degrades_to_passthrough_without_cognition(_no_cognition) -> None:
    # Even a marked body passes straight through when cognition can't be imported.
    body = {ENVELOPE_MARKER: 1, "input": {"a": 1}, "cognition": {}}
    agent_input, cognition = unwrap_cognition_request(body)
    assert agent_input is body
    assert cognition is None


def test_open_runtime_is_noop_without_cognition(_no_cognition) -> None:
    sink: list[dict] = []
    with open_cognition_runtime({"memory_digest": "d"}, sink):
        pass  # no channel — sink stays empty, no error
    assert sink == []


def test_open_runtime_routes_audit_when_cognition_present() -> None:
    import agent_cognition.tools.channel as ch

    sink: list[dict] = []
    with open_cognition_runtime({"memory_digest": "d"}, sink):
        assert ch.get_cognition_context() == {"memory_digest": "d"}
        ch.record_tool_audit({"tool_id": "t", "ok": True})
    assert sink == [{"tool_id": "t", "ok": True}]
    # Channel is reset on exit.
    assert ch.get_cognition_context() is None
