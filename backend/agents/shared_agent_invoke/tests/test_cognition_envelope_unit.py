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

    driven = maybe_drive_tool_loop(
        {"normal": "output"}, agent_id="a", source_run_id="r", cognition=None
    )
    assert driven == {"output": {"normal": "output"}, "tool_calls": [], "events": [], "error": None}


def test_maybe_drive_degrades_without_cognition(_no_cognition) -> None:
    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    # Cognition package unavailable → pass the result through with an empty audit.
    driven = maybe_drive_tool_loop(
        {"x": 1}, agent_id="a", source_run_id="r", cognition={"rules": []}
    )
    assert driven["output"] == {"x": 1}
    assert driven["tool_calls"] == [] and driven["events"] == [] and driven["error"] is None


def test_maybe_drive_lifts_writeback_events() -> None:
    # A pure-LLM entrypoint returns a marker-wrapped writeback; its episodic events
    # must reach the driven result (and so the response's memory_events).
    from agent_cognition.tools.envelope import wrap_writeback
    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    wrapped = wrap_writeback(
        {"output": "hi"},
        {"events": [{"id": "e1", "kind": "outcome"}], "tool_calls": [{"tool_id": "git"}]},
    )
    driven = maybe_drive_tool_loop(wrapped, agent_id="a", source_run_id="r", cognition=None)
    assert driven["output"] == {"output": "hi"}  # caller gets the inner output only
    assert driven["events"] == [{"id": "e1", "kind": "outcome"}]
    assert driven["tool_calls"] == [{"tool_id": "git"}]
    assert driven["error"] is None


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
    driven = maybe_drive_tool_loop(
        plan, agent_id="a", source_run_id="r", cognition={"rules": [{"bad": "rule"}]}
    )
    assert driven["output"] == {"done": True}
    assert side == [{}]  # the tool ran — the bad rule was ignored, not enforced
    assert driven["tool_calls"][0]["ok"] is True
    # The episodic memory events are returned for the proxy to persist (not dropped).
    assert driven["events"], "brokered memory events must be returned to the shim"
    assert any(ev["content"] == "echo" for ev in driven["events"])
    assert driven["error"] is None


def test_maybe_drive_preserves_partial_audit_on_loop_failure() -> None:
    from agent_cognition.tools.binding import BoundTool, BoundToolset, ExecutionSite
    from agent_cognition.tools.runner import ToolLoopPlan
    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    side: list = []

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
            raise RuntimeError("loop blew up after a side effect")

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

    driven = maybe_drive_tool_loop(plan, agent_id="a", source_run_id="r", cognition=None)
    # The loop failed, but the side effect already happened — its trusted audit
    # must survive (not be dropped) so the platform records what actually ran.
    assert side == [{}]
    assert driven["error"] and "loop failed" in driven["error"]
    assert driven["tool_calls"] and driven["tool_calls"][0]["tool_id"] == "echo"
    assert driven["events"]


def test_maybe_drive_deadline_blocks_dispatch() -> None:
    import time

    from shared_agent_invoke.cognition_envelope import maybe_drive_tool_loop

    side: list = []
    plan = _probe_plan(side)
    # A deadline already in the past → the broker refuses to dispatch the handler.
    driven = maybe_drive_tool_loop(
        plan, agent_id="a", source_run_id="r", cognition=None, deadline=time.monotonic() - 1
    )
    assert side == []  # no handler ran after the deadline
    assert driven["tool_calls"][0]["ok"] is False
    assert "deadline" in (driven["tool_calls"][0]["error"] or "")


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
    driven = maybe_drive_tool_loop(plan, agent_id="a", source_run_id="r", cognition=cognition)
    assert driven["output"] == {"done": True}
    assert side == []  # the forbidden tool never ran
    calls = driven["tool_calls"]
    assert calls and calls[0]["ok"] is False and calls[0]["function"] == "echo"


def test_dump_audit_handles_none() -> None:
    from shared_agent_invoke.cognition_envelope import dump_audit

    assert dump_audit(None) == ([], [])


def test_new_tool_audit_is_none_without_cognition(_no_cognition) -> None:
    from shared_agent_invoke.cognition_envelope import new_tool_audit

    assert new_tool_audit() is None


def test_shared_audit_is_populated_for_timeout_snapshot() -> None:
    # The shim passes a caller-owned audit so it can snapshot the partial record
    # even if the invoke times out mid-loop. Driving fills that very object.
    from shared_agent_invoke.cognition_envelope import (
        dump_audit,
        maybe_drive_tool_loop,
        new_tool_audit,
    )

    side: list = []
    plan = _probe_plan(side)
    audit = new_tool_audit()
    assert audit is not None
    maybe_drive_tool_loop(plan, agent_id="a", source_run_id="r", cognition=None, audit=audit)
    # The shim's reference is populated and snapshot-able (what the 504 path reads).
    tool_calls, events = dump_audit(audit)
    assert tool_calls and tool_calls[0]["tool_id"] == "echo"
    assert events
