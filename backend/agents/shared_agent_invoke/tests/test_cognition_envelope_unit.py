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
    """Make the lazy ``agent_cognition`` imports the bridge does all fail."""
    for mod in (
        "agent_cognition.tools.envelope",
        "agent_cognition.tools.channel",
        "agent_cognition.tools.runner",
        "agent_cognition.models",
    ):
        monkeypatch.setitem(sys.modules, mod, None)
    yield


def test_unwrap_degrades_to_passthrough_without_cognition(_no_cognition) -> None:
    # Even a marked body passes straight through when cognition can't be imported.
    body = {ENVELOPE_MARKER: 1, "input": {"a": 1}, "cognition": {}}
    agent_input, cognition = unwrap_cognition_request(body)
    assert agent_input is body
    assert cognition is None


def test_open_runtime_is_noop_without_cognition(_no_cognition) -> None:
    with open_cognition_runtime({"memory_digest": "d"}):
        pass  # no channel — no error
    # get_cognition_context would be unavailable; the point is it doesn't raise.


def test_open_runtime_exposes_cognition_side_channel() -> None:
    import agent_cognition.tools.channel as ch

    with open_cognition_runtime({"memory_digest": "d", "rules": []}):
        assert ch.get_cognition_context() == {"memory_digest": "d", "rules": []}
    # Channel is reset on exit.
    assert ch.get_cognition_context() is None


def test_maybe_drive_passes_through_non_plan_result() -> None:
    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    out, audit = maybe_drive_tool_loop(
        {"normal": "output"}, agent_id="a", source_run_id="r", cognition=None
    )
    assert out == {"normal": "output"}
    assert audit == []


def test_maybe_drive_degrades_without_cognition(_no_cognition) -> None:
    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    # Cognition package unavailable → pass the result through with an empty audit.
    out, audit = maybe_drive_tool_loop(
        {"x": 1}, agent_id="a", source_run_id="r", cognition={"rules": []}
    )
    assert out == {"x": 1}
    assert audit == []


def _probe_plan(side: list):
    from agent_cognition.tools.binding import BoundTool, BoundToolset, ExecutionSite
    from agent_cognition.tools.runner import ToolLoopPlan

    class _LLM:
        def __init__(self) -> None:
            self.n = 0

        def chat(self, messages, **_kw):
            self.n += 1
            if self.n == 1:
                return {
                    "__tool_calls__": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "echo", "arguments": {}},
                        }
                    ]
                }
            return {"done": True}

    defn = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "e",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    }
    toolset = BoundToolset(
        (
            BoundTool(
                tool_id="echo",
                site=ExecutionSite.SANDBOX_LOCAL,
                definitions=(defn,),
                handlers={"echo": lambda a: side.append(a) or {"ok": True}},
            ),
        )
    )
    return ToolLoopPlan(llm=_LLM(), system_prompt="s", user_prompt="u", toolset=toolset)


def test_maybe_drive_skips_malformed_cognition_rules() -> None:
    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    side: list = []
    plan = _probe_plan(side)
    # A malformed rule dict must be skipped (never block the run), so the tool runs.
    out, audit = maybe_drive_tool_loop(
        plan, agent_id="a", source_run_id="r", cognition={"rules": [{"bad": "rule"}]}
    )
    assert out == {"done": True}
    assert side == [{}]  # the tool ran — the bad rule was ignored, not enforced
    assert audit[0]["ok"] is True


def test_maybe_drive_runs_a_tool_loop_plan_and_sources_rules_from_cognition() -> None:
    from agent_cognition.tools.binding import BoundTool, BoundToolset, ExecutionSite
    from agent_cognition.tools.runner import ToolLoopPlan
    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    class _LLM:
        def __init__(self) -> None:
            self.n = 0

        def chat(self, messages, **_kw):
            self.n += 1
            if self.n == 1:
                return {
                    "__tool_calls__": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "echo", "arguments": {}},
                        }
                    ]
                }
            return {"done": True}

    side: list = []
    defn = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "e",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    }
    toolset = BoundToolset(
        (
            BoundTool(
                tool_id="echo",
                site=ExecutionSite.SANDBOX_LOCAL,
                definitions=(defn,),
                handlers={"echo": lambda a: side.append(a) or {"ok": True}},
            ),
        )
    )
    plan = ToolLoopPlan(llm=_LLM(), system_prompt="s", user_prompt="u", toolset=toolset)

    # An enforced forbid_tool rule injected via cognition must block the call —
    # proving the gate is sourced from the platform cognition block, not the plan.
    cognition = {
        "rules": [
            {
                "id": "r1",
                "agent_id": "a",
                "text": "no echo",
                "mode": "enforced",
                "status": "active",
                "predicate": {
                    "phase": "tool_gate",
                    "check": {"op": "forbid_tool", "tool_id": "echo"},
                },
                "source": "operator",
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
            }
        ]
    }
    out, audit = maybe_drive_tool_loop(plan, agent_id="a", source_run_id="r", cognition=cognition)
    assert out == {"done": True}
    assert side == []  # the forbidden tool never ran
    assert audit and audit[0]["ok"] is False and audit[0]["function"] == "echo"
