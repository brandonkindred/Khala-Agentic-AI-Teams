"""Validation / round-trip tests for the cognition domain models.

These require no Postgres and always run, so they carry the coverage for
``agent_cognition.models``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent_cognition import (
    CognitionContext,
    CognitionWriteback,
    EventKind,
    MemoryEvent,
    PeriodSummary,
    ProposalAction,
    ProposalStatus,
    Rule,
    RuleMode,
    RuleProposal,
    RuleSource,
    RuleStatus,
    Scale,
    ToolCall,
)

NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Enums — exact member sets are load-bearing (they map to TEXT columns).
# ---------------------------------------------------------------------------
def test_enum_member_values() -> None:
    assert {e.value for e in EventKind} == {
        "observation",
        "action",
        "tool_call",
        "outcome",
        "error",
        "feedback",
    }
    assert {s.value for s in Scale} == {"day", "week", "month", "year"}
    assert {m.value for m in RuleMode} == {"advisory", "enforced"}
    assert {s.value for s in RuleStatus} == {"active", "retired"}
    assert {s.value for s in RuleSource} == {"seed", "derived", "operator"}
    assert {a.value for a in ProposalAction} == {"add", "retire", "amend"}


def test_proposal_status_includes_superseded() -> None:
    # Acceptance criterion: the system auto-withdraw terminal state exists
    # and is distinct from the human ``rejected`` decision.
    assert {s.value for s in ProposalStatus} == {
        "pending",
        "approved",
        "rejected",
        "superseded",
    }
    assert ProposalStatus.SUPERSEDED.value == "superseded"


def test_enums_are_str_mixed() -> None:
    # str-mixed enums serialize as plain strings.
    assert EventKind.OBSERVATION == "observation"
    assert (
        MemoryEvent(
            id="e1",
            agent_id="a1",
            kind=EventKind.ACTION,
            occurred_at=NOW,
            source_run_id="r1",
            source_seq=0,
        ).model_dump()["kind"]
        == "action"
    )


# ---------------------------------------------------------------------------
# Model construction + round-trip.
# ---------------------------------------------------------------------------
def test_memory_event_round_trip_and_defaults() -> None:
    ev = MemoryEvent(
        id="e1",
        agent_id="a1",
        kind=EventKind.OBSERVATION,
        occurred_at=NOW,
        source_run_id="run-1",
        source_seq=3,
    )
    # Defaults applied.
    assert ev.content == ""
    assert ev.data == {}
    assert ev.salience == 0.0
    again = MemoryEvent.model_validate(ev.model_dump())
    assert again == ev


def test_period_summary_round_trip_and_defaults() -> None:
    s = PeriodSummary(
        id="s1",
        agent_id="a1",
        scale=Scale.MONTH,
        period_start=NOW,
        period_end=NOW,
        created_at=NOW,
    )
    assert s.version == 1
    assert s.stale is False
    assert s.source_count == 0
    assert s.covers_through is None
    assert s.highlights == []
    assert PeriodSummary.model_validate(s.model_dump()) == s


def test_rule_round_trip_and_defaults() -> None:
    r = Rule(
        id="r1",
        agent_id="a1",
        text="never delete prod",
        mode=RuleMode.ENFORCED,
        status=RuleStatus.ACTIVE,
        source=RuleSource.SEED,
        created_at=NOW,
        updated_at=NOW,
    )
    assert r.predicate == {}
    assert r.evidence == []
    assert r.needs_review is False
    assert r.priority == 0
    assert r.rationale is None
    assert Rule.model_validate(r.model_dump()) == r


def test_rule_proposal_round_trip_and_defaults() -> None:
    p = RuleProposal(
        id="p1",
        agent_id="a1",
        action=ProposalAction.ADD,
        created_at=NOW,
    )
    assert p.status is ProposalStatus.PENDING
    assert p.target_rule_id is None
    assert p.proposed_rule is None
    assert p.stale_evidence is False
    assert p.decided_by is None
    assert p.decided_at is None
    assert RuleProposal.model_validate(p.model_dump()) == p


def test_rule_proposal_amend_carries_target_and_replacement() -> None:
    p = RuleProposal(
        id="p2",
        agent_id="a1",
        action=ProposalAction.AMEND,
        target_rule_id="r1",
        proposed_rule={"text": "new", "mode": "advisory"},
        evidence=[["s1", 2]],
        status=ProposalStatus.SUPERSEDED,
        decided_by="operator-x",
        decided_at=NOW,
        created_at=NOW,
    )
    assert p.target_rule_id == "r1"
    assert p.proposed_rule == {"text": "new", "mode": "advisory"}
    assert RuleProposal.model_validate(p.model_dump()) == p


def test_tool_call_round_trip_and_defaults() -> None:
    tc = ToolCall(tool_id="git")
    assert tc.args == {}
    assert tc.ok is True
    assert tc.result is None
    assert tc.error is None
    assert tc.occurred_at is None
    populated = ToolCall(
        tool_id="http_api",
        args={"url": "https://example.com"},
        ok=False,
        result={"status": 500},
        error="boom",
        occurred_at=NOW,
    )
    assert ToolCall.model_validate(populated.model_dump()) == populated


def test_cognition_context_defaults_and_nesting() -> None:
    ctx = CognitionContext()
    assert ctx.rules == []
    assert ctx.memory_digest == ""
    rule = Rule(
        id="r1",
        agent_id="a1",
        text="t",
        mode=RuleMode.ADVISORY,
        status=RuleStatus.ACTIVE,
        source=RuleSource.OPERATOR,
        created_at=NOW,
        updated_at=NOW,
    )
    ctx2 = CognitionContext(rules=[rule], memory_digest="recent: ...")
    again = CognitionContext.model_validate(ctx2.model_dump())
    assert again.rules[0].id == "r1"
    assert again.memory_digest == "recent: ..."


def test_cognition_writeback_defaults_and_nesting() -> None:
    wb = CognitionWriteback()
    assert wb.events == []
    assert wb.tool_calls == []
    assert wb.truncated is False
    wb2 = CognitionWriteback(
        events=[
            MemoryEvent(
                id="e1",
                agent_id="a1",
                kind=EventKind.OUTCOME,
                occurred_at=NOW,
                source_run_id="r1",
                source_seq=0,
            )
        ],
        tool_calls=[ToolCall(tool_id="git")],
        truncated=True,
    )
    again = CognitionWriteback.model_validate(wb2.model_dump())
    assert again.events[0].id == "e1"
    assert again.tool_calls[0].tool_id == "git"
    assert again.truncated is True


# ---------------------------------------------------------------------------
# Validation failures (preconditions enforced at construction).
# ---------------------------------------------------------------------------
def test_invalid_enum_value_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryEvent(
            id="e1",
            agent_id="a1",
            kind="not_a_kind",  # type: ignore[arg-type]
            occurred_at=NOW,
            source_run_id="r1",
            source_seq=0,
        )


def test_missing_required_field_rejected() -> None:
    with pytest.raises(ValidationError):
        Rule(  # type: ignore[call-arg]
            id="r1",
            agent_id="a1",
            text="t",
            mode=RuleMode.ADVISORY,
            status=RuleStatus.ACTIVE,
            # source missing
            created_at=NOW,
            updated_at=NOW,
        )
