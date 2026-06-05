"""Tests for the brokered tool loop (Step 7).

Uses a scripted fake LLM (the ``chat`` protocol of
``llm_service.tool_loop.complete_json_with_tool_loop``) and fake handlers — no
Postgres, no real model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent_cognition.models import (
    EventKind,
    Rule,
    RuleMode,
    RuleSource,
    RuleStatus,
)
from agent_cognition.tools.binding import BoundTool, BoundToolset, ExecutionSite
from agent_cognition.tools.runner import drive_platform_bound_loop, run_tool_loop

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class ScriptedLLM:
    """Returns scripted ``chat`` results and records the messages it was sent."""

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.seen_messages: list[list[dict]] = []

    def chat(self, messages, **_kwargs):
        self.seen_messages.append([dict(m) for m in messages])
        return self._script.pop(0)


def _tool_call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "__tool_calls__": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ]
    }


def _enforced_rule(predicate: dict, *, rid: str = "r1", text: str = "rule") -> Rule:
    return Rule(
        id=rid,
        agent_id="agent-x",
        text=text,
        mode=RuleMode.ENFORCED,
        status=RuleStatus.ACTIVE,
        predicate=predicate,
        rationale=None,
        source=RuleSource.OPERATOR,
        evidence=[],
        needs_review=False,
        priority=0,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _toolset(
    name: str, handler, *, site: ExecutionSite = ExecutionSite.SANDBOX_LOCAL
) -> BoundToolset:
    definition = {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    }
    return BoundToolset(
        (BoundTool(tool_id=name, site=site, definitions=(definition,), handlers={name: handler}),)
    )


def _multi_fn_toolset(tool_id: str, function_name: str, handler) -> BoundToolset:
    """A tool whose declared id differs from its advertised function name.

    Mirrors `git` (tool_id=`git`, functions `git_status` / `git_commit` / …).
    """
    definition = {
        "type": "function",
        "function": {
            "name": function_name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    }
    return BoundToolset(
        (
            BoundTool(
                tool_id=tool_id,
                site=ExecutionSite.SANDBOX_LOCAL,
                definitions=(definition,),
                handlers={function_name: handler},
            ),
        )
    )


def _run(llm, toolset, enforced_rules=None, **kw):
    return run_tool_loop(
        llm,
        agent_id="agent-x",
        source_run_id="run-1",
        user_prompt="do it",
        system_prompt="be good",
        toolset=toolset,
        enforced_rules=enforced_rules or [],
        clock=lambda: _NOW,
        **kw,
    )


# ---------------------------------------------------------------------------
# Each tool call writes memory events
# ---------------------------------------------------------------------------
def test_tool_call_writes_memory_events() -> None:
    calls = []

    def echo(args):
        calls.append(args)
        return {"echoed": args}

    llm = ScriptedLLM([_tool_call("echo", {"x": 1}), {"final": True}])
    result, audit = _run(llm, _toolset("echo", echo))

    assert result == {"final": True}
    assert calls == [{"x": 1}]
    # One tool_call (intent) + one outcome event; one ToolCall summary.
    kinds = [e.kind for e in audit.events]
    assert kinds == [EventKind.TOOL_CALL, EventKind.OUTCOME]
    assert [e.source_seq for e in audit.events] == [0, 1]
    assert len(audit.tool_calls) == 1
    tc = audit.tool_calls[0]
    assert tc.tool_id == "echo" and tc.ok is True
    assert tc.result == {"echoed": {"x": 1}}


def test_source_seq_starts_at_offset() -> None:
    llm = ScriptedLLM([_tool_call("echo", {}), {"final": True}])
    _result, audit = _run(llm, _toolset("echo", lambda a: a), source_seq_start=10)
    assert [e.source_seq for e in audit.events] == [10, 11]


# ---------------------------------------------------------------------------
# forbid_tool is refused BEFORE the handler runs (no side effect)
# ---------------------------------------------------------------------------
def test_forbidden_tool_refused_before_dispatch() -> None:
    side_effects = []

    def dangerous(args):
        side_effects.append(args)  # must never run
        return {"ran": True}

    rule = _enforced_rule(
        {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "dangerous"}},
        text="never run dangerous",
    )
    # The model calls the forbidden tool, sees the refusal, then returns final.
    llm = ScriptedLLM([_tool_call("dangerous", {"k": 1}), {"final": True}])
    result, audit = _run(llm, _toolset("dangerous", dangerous), enforced_rules=[rule])

    assert side_effects == []  # handler never dispatched
    assert result == {"final": True}
    # The blocked call is recorded (trusted audit) as a single tool_call event.
    assert [e.kind for e in audit.events] == [EventKind.TOOL_CALL]
    assert audit.events[0].data["blocked"] is True
    assert audit.tool_calls[0].ok is False
    assert "never run dangerous" in (audit.tool_calls[0].error or "")
    # The model was handed a structured refusal as the tool result.
    tool_msgs = [m for round_ in llm.seen_messages for m in round_ if m.get("role") == "tool"]
    assert any("forbidden_by_rule" in m["content"] for m in tool_msgs)


def test_forbid_tool_matches_declared_id_not_function_name() -> None:
    # Regression: enforced predicates name the declared tool id (`git`), while the
    # advertised handler is a function (`git_write_files_and_commit`). Gating on
    # the function name would let the forbidden call dispatch.
    side_effects = []

    def git_write(args):
        side_effects.append(args)
        return {"committed": True}

    rule = _enforced_rule(
        {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "git"}},
        text="no git writes",
    )
    llm = ScriptedLLM([_tool_call("git_write_files_and_commit", {"files": {}}), {"final": True}])
    result, audit = _run(
        llm,
        _multi_fn_toolset("git", "git_write_files_and_commit", git_write),
        enforced_rules=[rule],
    )

    assert side_effects == []  # the forbidden git function never dispatched
    assert result == {"final": True}
    assert audit.events[0].data["blocked"] is True
    # The trusted audit carries the declared tool id (the gate identity) and the
    # specific function that was attempted.
    assert audit.tool_calls[0].tool_id == "git"
    assert audit.events[0].content == "git_write_files_and_commit"
    assert audit.events[0].data["tool_id"] == "git"


def test_allowed_call_records_declared_tool_id_and_function() -> None:
    llm = ScriptedLLM([_tool_call("git_status", {}), {"final": True}])
    _result, audit = _run(llm, _multi_fn_toolset("git", "git_status", lambda a: {"clean": True}))
    outcome = audit.events[-1]
    assert outcome.kind is EventKind.OUTCOME
    assert outcome.content == "git_status"  # function name preserved
    assert outcome.data["tool_id"] == "git"  # declared id in the record
    # The out-of-band ToolCall audit carries BOTH the declared id (gate identity)
    # and the specific function that ran, so multi-function tools don't collapse.
    assert audit.tool_calls[0].tool_id == "git"
    assert audit.tool_calls[0].function == "git_status"


def test_audit_preserves_function_across_call_outcomes() -> None:
    # Blocked, errored, and successful calls all record the specific function.
    forbid = _enforced_rule(
        {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "git"}}
    )
    llm = ScriptedLLM([_tool_call("git_status", {}), {"final": True}])
    _r, blocked_audit = _run(
        llm, _multi_fn_toolset("git", "git_status", lambda a: a), enforced_rules=[forbid]
    )
    assert blocked_audit.tool_calls[0].function == "git_status"

    def boom(_a):
        raise RuntimeError("x")

    llm = ScriptedLLM([_tool_call("git_commit", {}), {"final": True}])
    _r, err_audit = _run(llm, _multi_fn_toolset("git", "git_commit", boom))
    assert err_audit.tool_calls[0].function == "git_commit"


def test_unrelated_enforced_rule_does_not_block() -> None:
    rule = _enforced_rule(
        {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "something_else"}}
    )
    llm = ScriptedLLM([_tool_call("echo", {"x": 1}), {"final": True}])
    result, audit = _run(llm, _toolset("echo", lambda a: {"ok": a}), enforced_rules=[rule])
    assert result == {"final": True}
    assert any(e.kind is EventKind.OUTCOME for e in audit.events)


# ---------------------------------------------------------------------------
# Handler exceptions become error events, loop continues
# ---------------------------------------------------------------------------
def test_handler_exception_records_error_event() -> None:
    def boom(_args):
        raise RuntimeError("kaboom")

    llm = ScriptedLLM([_tool_call("boom", {}), {"final": True}])
    result, audit = _run(llm, _toolset("boom", boom))
    assert result == {"final": True}
    assert [e.kind for e in audit.events] == [EventKind.TOOL_CALL, EventKind.ERROR]
    assert audit.tool_calls[0].ok is False
    # Only the exception *type* is recorded — never the raw message.
    assert audit.tool_calls[0].error == "RuntimeError"


def test_handler_exception_message_is_not_leaked() -> None:
    # A handler whose exception text embeds a secret must not leak it to the
    # model (the tool result) or the trusted audit.
    secret = "token=SECRET-LEAK-123"

    def boom(_args):
        raise RuntimeError(secret)

    llm = ScriptedLLM([_tool_call("boom", {}), {"final": True}])
    _result, audit = _run(llm, _toolset("boom", boom))
    blob = json.dumps([e.model_dump(mode="json") for e in audit.events])
    blob += json.dumps([tc.model_dump(mode="json") for tc in audit.tool_calls])
    assert "SECRET-LEAK-123" not in blob
    # The model only sees a generic, type-tagged refusal — not the raw message.
    tool_msgs = [m for round_ in llm.seen_messages for m in round_ if m.get("role") == "tool"]
    joined = "".join(m["content"] for m in tool_msgs)
    assert "SECRET-LEAK-123" not in joined
    assert "handler_exception" in joined and "RuntimeError" in joined


def test_handler_failure_result_marks_outcome_not_ok() -> None:
    llm = ScriptedLLM([_tool_call("op", {}), {"final": True}])
    result, audit = _run(llm, _toolset("op", lambda a: {"success": False, "error": "nope"}))
    assert result == {"final": True}
    # A {"success": False} result is logged as an error-kind outcome.
    assert audit.events[-1].kind is EventKind.ERROR
    assert audit.tool_calls[0].ok is False


# ---------------------------------------------------------------------------
# Secret stripping
# ---------------------------------------------------------------------------
def test_secrets_are_stripped_from_memory() -> None:
    def login(args):
        return {"token": "super-secret-value", "ok": True}

    llm = ScriptedLLM(
        [_tool_call("login", {"password": "hunter2", "user": "bob"}), {"final": True}]
    )
    _result, audit = _run(llm, _toolset("login", login))
    blob = json.dumps([e.model_dump(mode="json") for e in audit.events])
    assert "hunter2" not in blob
    assert "super-secret-value" not in blob
    assert "bob" in blob  # non-secret args are retained


# ---------------------------------------------------------------------------
# Audit is a return value only — no ambient channel
# ---------------------------------------------------------------------------
def test_audit_is_returned_not_ambient() -> None:
    # The audit lives only on the return value; there is no module-level sink the
    # loop writes to (so agent code has nothing to forge into).
    import agent_cognition.tools.channel as ch

    llm = ScriptedLLM([_tool_call("echo", {"x": 1}), {"final": True}])
    result, audit = _run(llm, _toolset("echo", lambda a: a))
    assert result == {"final": True}
    assert len(audit.tool_calls) == 1
    assert audit.tool_calls[0].tool_id == "echo"
    assert not hasattr(ch, "_record_brokered")  # no ambient writer exists


def test_execute_plan_drives_the_loop_and_returns_audit() -> None:
    from agent_cognition.tools.runner import ToolLoopPlan, execute_plan

    calls = []

    def echo(args):
        calls.append(args)
        return {"echoed": args}

    plan = ToolLoopPlan(
        llm=ScriptedLLM([_tool_call("echo", {"x": 1}), {"final": True}]),
        system_prompt="sys",
        user_prompt="do",
        toolset=_toolset("echo", echo),
    )
    out, audit = execute_plan(plan, agent_id="a", source_run_id="r", enforced_rules=[])
    assert out == {"final": True}
    assert calls == [{"x": 1}]
    assert audit.tool_calls[0].tool_id == "echo"


def test_execute_plan_enforces_caller_rules_not_agent_supplied() -> None:
    # The gate is authoritative: enforced rules come from the caller (the shim,
    # sourced from cognition), never from the agent's plan — so a forbidden tool
    # is blocked even though the plan can't carry rules.
    from agent_cognition.tools.runner import ToolLoopPlan, execute_plan

    forbid = _enforced_rule(
        {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "echo"}}
    )
    side: list = []
    plan = ToolLoopPlan(
        llm=ScriptedLLM([_tool_call("echo", {}), {"final": True}]),
        system_prompt="s",
        user_prompt="u",
        toolset=_toolset("echo", lambda a: side.append(a) or {"ran": True}),
    )
    _out, audit = execute_plan(plan, agent_id="a", source_run_id="r", enforced_rules=[forbid])
    assert side == []  # forbidden tool never dispatched
    assert audit.tool_calls[0].ok is False


# ---------------------------------------------------------------------------
# Platform-bound proxy-driven loop (stubbed runtime) — no secret exposure
# ---------------------------------------------------------------------------
def test_platform_bound_loop_keeps_secret_platform_side() -> None:
    secret = "PLATFORM-ONLY-API-KEY"

    def http_call(args):
        # The handler *uses* the secret internally but never returns it.
        assert secret  # platform-side credential
        return {"status": 200, "body": f"fetched {args.get('url')}"}

    toolset = _toolset("http__call", http_call, site=ExecutionSite.PLATFORM_BOUND)
    runtime = ScriptedLLM([_tool_call("http__call", {"url": "/data"}), {"final": "done"}])
    result, audit = drive_platform_bound_loop(
        runtime,
        agent_id="agent-x",
        source_run_id="run-1",
        user_prompt="fetch",
        system_prompt="sys",
        toolset=toolset,
        enforced_rules=[],
        clock=lambda: _NOW,
    )
    assert result == {"final": "done"}
    assert audit.tool_calls[0].ok is True
    # The secret never crossed back to the (sandbox) runtime in any message.
    exchanged = json.dumps(runtime.seen_messages)
    assert secret not in exchanged
    # But the tool genuinely ran (its result reached the runtime).
    assert "fetched /data" in exchanged


def test_default_clock_is_used_when_not_injected() -> None:
    # Exercise the real _utcnow default (no clock= override).
    llm = ScriptedLLM([_tool_call("echo", {}), {"final": True}])
    _result, audit = run_tool_loop(
        llm,
        agent_id="agent-x",
        source_run_id="run-1",
        user_prompt="do it",
        system_prompt="be good",
        toolset=_toolset("echo", lambda a: a),
        enforced_rules=[],
    )
    assert audit.events[0].occurred_at.tzinfo is not None


# ---------------------------------------------------------------------------
# _sanitize internals
# ---------------------------------------------------------------------------
def test_sanitize_covers_all_branches() -> None:
    from agent_cognition.tools.runner import _MAX_STR, _sanitize

    class Weird:
        def __repr__(self) -> str:
            return "W" * (_MAX_STR + 10)

    out = _sanitize(
        {
            "password": "secret",  # redacted
            "keep": "ok",  # plain str
            "items": [1, 2, {"token": "x"}],  # list + nested secret
            "long": "a" * (_MAX_STR + 5),  # truncated string
            "obj": Weird(),  # unknown object → truncated repr
            "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}},  # exceeds depth
        }
    )
    assert out["password"] == "***"
    assert out["keep"] == "ok"
    assert out["items"][2]["token"] == "***"
    assert out["long"].endswith("…<truncated>")
    assert out["obj"].endswith("…<truncated>")
    # The deepest level is replaced with the depth sentinel.
    assert "<truncated:depth>" in repr(out["deep"])


def test_sanitize_bounds_large_collections_to_max_items() -> None:
    from agent_cognition.tools.runner import _MAX_ITEMS, _sanitize

    big_dict = {f"k{i}": i for i in range(_MAX_ITEMS + 25)}
    big_list = list(range(_MAX_ITEMS + 25))
    # The cap is applied during traversal (islice), so the output is bounded.
    assert len(_sanitize(big_dict)) == _MAX_ITEMS
    assert len(_sanitize(big_list)) == _MAX_ITEMS


def test_drive_platform_bound_rejects_non_platform_tool() -> None:
    toolset = _toolset("echo", lambda a: a, site=ExecutionSite.SANDBOX_LOCAL)
    with pytest.raises(AssertionError, match="not platform_bound"):
        drive_platform_bound_loop(
            ScriptedLLM([{"final": True}]),
            agent_id="agent-x",
            source_run_id="run-1",
            user_prompt="x",
            system_prompt="y",
            toolset=toolset,
            enforced_rules=[],
        )
