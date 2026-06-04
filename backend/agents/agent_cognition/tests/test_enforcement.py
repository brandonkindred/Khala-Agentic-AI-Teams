"""Pure-Python tests for the rules enforcement layer (Step 5).

No Postgres needed — enforcement takes already-fetched rules as arguments, so
these always run.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_cognition.models import Rule, RuleMode, RuleSource, RuleStatus
from agent_cognition.rules.enforcement import (
    build_rule_prompt_block,
    evaluate_postcondition,
    evaluate_precondition,
    evaluate_tool_call,
)

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _rule(
    *,
    rid: str = "r1",
    text: str = "rule",
    mode: RuleMode = RuleMode.ENFORCED,
    status: RuleStatus = RuleStatus.ACTIVE,
    predicate: dict | None = None,
    rationale: str | None = None,
    priority: int = 0,
) -> Rule:
    return Rule(
        id=rid,
        agent_id="a",
        text=text,
        mode=mode,
        status=status,
        predicate=predicate or {},
        rationale=rationale,
        source=RuleSource.OPERATOR,
        evidence=[],
        needs_review=False,
        priority=priority,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Advisory prompt block
# ---------------------------------------------------------------------------
def test_prompt_block_empty_when_no_advisory() -> None:
    assert build_rule_prompt_block([]) == ""
    # enforced + retired-advisory both filtered out
    rules = [
        _rule(mode=RuleMode.ENFORCED),
        _rule(rid="r2", mode=RuleMode.ADVISORY, status=RuleStatus.RETIRED),
    ]
    assert build_rule_prompt_block(rules) == ""


def test_prompt_block_orders_and_renders() -> None:
    rules = [
        _rule(rid="b", mode=RuleMode.ADVISORY, text="low", priority=1),
        _rule(rid="a", mode=RuleMode.ADVISORY, text="high", priority=5, rationale="why"),
    ]
    lines = build_rule_prompt_block(rules).splitlines()
    assert lines[0] == "## Operating rules"
    assert lines[1] == "- high (rationale: why)"  # priority desc
    assert lines[2] == "- low"


# ---------------------------------------------------------------------------
# Precondition / postcondition / tool gate
# ---------------------------------------------------------------------------
def test_precondition_no_rules_allows() -> None:
    assert evaluate_precondition({"input": {}}, []) == (True, None)


def test_precondition_block_and_allow() -> None:
    rule = _rule(
        text="temp cap",
        predicate={
            "phase": "precondition",
            "check": {"op": "<=", "path": "input.temperature", "value": 0.7},
        },
    )
    assert evaluate_precondition({"input": {"temperature": 0.5}}, [rule]) == (True, None)
    holds, reason = evaluate_precondition({"input": {"temperature": 0.9}}, [rule])
    assert holds is False
    assert reason is not None and reason.startswith("temp cap:")


def test_precondition_ignores_non_matching_rules() -> None:
    advisory = _rule(
        mode=RuleMode.ADVISORY,
        predicate={"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}},
    )
    toolgate = _rule(
        rid="t", predicate={"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "x"}}
    )
    retired = _rule(
        rid="z",
        status=RuleStatus.RETIRED,
        predicate={"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}},
    )
    # none apply to the precondition phase (would block on a missing path if they did)
    assert evaluate_precondition({"input": {}}, [advisory, toolgate, retired]) == (True, None)


def test_precondition_first_failure_by_priority() -> None:
    low = _rule(
        rid="r1",
        text="low",
        priority=1,
        predicate={"phase": "precondition", "check": {"op": "==", "path": "input.a", "value": 1}},
    )
    high = _rule(
        rid="r2",
        text="high",
        priority=5,
        predicate={"phase": "precondition", "check": {"op": "==", "path": "input.b", "value": 2}},
    )
    holds, reason = evaluate_precondition({"input": {"a": 9, "b": 9}}, [low, high])
    assert holds is False
    assert reason is not None and reason.startswith("high:")  # higher priority evaluated first


def test_postcondition_wraps_output() -> None:
    rule = _rule(
        predicate={
            "phase": "postcondition",
            "check": {"op": "==", "path": "output.status", "value": "ok"},
        }
    )
    assert evaluate_postcondition({"status": "ok"}, [rule]) == (True, None)
    holds, _reason = evaluate_postcondition({"status": "err"}, [rule])
    assert holds is False


def test_tool_call_forbid() -> None:
    rule = _rule(
        predicate={"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "shell"}}
    )
    assert evaluate_tool_call("git", {}, [rule]) == (True, None)
    holds, _reason = evaluate_tool_call("shell", {}, [rule])
    assert holds is False


def test_tool_call_arg_conditioned() -> None:
    rule = _rule(
        predicate={
            "phase": "tool_gate",
            "check": {
                "op": "not",
                "of": [
                    {
                        "op": "all",
                        "of": [
                            {"op": "==", "path": "tool_id", "value": "http"},
                            {"op": "==", "path": "args.method", "value": "DELETE"},
                        ],
                    }
                ],
            },
        }
    )
    assert evaluate_tool_call("http", {"method": "GET"}, [rule]) == (True, None)
    assert evaluate_tool_call("http", {"method": "DELETE"}, [rule])[0] is False


def test_tool_call_requires_tool_id() -> None:
    with pytest.raises(AssertionError):
        evaluate_tool_call("", {}, [])


def test_phaseless_enforced_rule_fails_closed_at_every_gate() -> None:
    # An active enforced rule whose predicate declares no recognized phase belongs
    # to no gate; it must block everywhere (defense in depth), not be silently
    # dropped — even though the store rejects such a rule on write.
    orphan = _rule(text="orphan", mode=RuleMode.ENFORCED, predicate={})
    for holds, reason in (
        evaluate_precondition({"input": {}}, [orphan]),
        evaluate_postcondition({}, [orphan]),
        evaluate_tool_call("t", {}, [orphan]),
    ):
        assert holds is False
        assert reason is not None and "no enforceable phase" in reason
    # A well-formed enforced rule for a DIFFERENT phase is still skipped (not
    # blocked) by an unrelated gate.
    pc = _rule(
        rid="pc",
        predicate={
            "phase": "postcondition",
            "check": {"op": "==", "path": "output.s", "value": "ok"},
        },
    )
    assert evaluate_precondition({"input": {}}, [pc]) == (True, None)


def test_malformed_enforced_predicate_blocks() -> None:
    rule = _rule(
        text="bad",
        predicate={
            "phase": "precondition",
            "check": {"op": "bogus", "path": "input.x", "value": 1},
        },
    )
    holds, reason = evaluate_precondition({"input": {"x": 1}}, [rule])
    assert holds is False
    assert reason is not None and "invalid predicate" in reason


def test_enforcement_reexports_predicate_validators() -> None:
    from agent_cognition.rules.enforcement import (
        PredicateError,
        is_valid_predicate,
        validate_predicate,
    )

    assert (
        is_valid_predicate(
            {"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}}
        )
        is True
    )
    with pytest.raises(PredicateError):
        validate_predicate({"phase": "bad", "check": {}})


def test_postcondition_neq_allows_when_field_absent() -> None:
    # A 'block when output.error == fatal' rule, written as != , must allow a
    # successful output that simply has no error key (missing-path semantics).
    rule = _rule(
        predicate={
            "phase": "postcondition",
            "check": {"op": "!=", "path": "output.error", "value": "fatal"},
        }
    )
    assert evaluate_postcondition({"result": 1}, [rule]) == (True, None)
    assert evaluate_postcondition({"error": "fatal"}, [rule])[0] is False


def test_enforced_gate_does_not_raise_on_exotic_value() -> None:
    # An enforced predicate carrying a non-JSON-serializable value (e.g. a set)
    # must not crash the gate; it parses and evaluation returns a normal verdict.
    rule = _rule(
        predicate={
            "phase": "precondition",
            "check": {"op": "==", "path": "input.x", "value": {1, 2}},
        }
    )
    holds, _reason = evaluate_precondition({"input": {"x": 5}}, [rule])
    assert holds is False  # 5 != {1, 2}; no exception raised
