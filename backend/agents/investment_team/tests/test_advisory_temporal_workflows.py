"""Unit tests for the interactive advisory workflows.

Each is a thin execute-and-wait driver over one activity. Driven with
``asyncio.run`` and a patched ``temporalio.workflow.execute_activity`` (no live
server), asserting the workflow delegates to the right activity and returns its
result verbatim.
"""

from __future__ import annotations

import asyncio

import pytest


def _patch_execute(monkeypatch, result):
    calls = []

    async def _fake_exec(fn, *, args, **kw):
        calls.append((fn.__name__, args, kw))
        return result

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    return calls


@pytest.mark.parametrize(
    "workflow_attr, activity_name",
    [
        ("CreateProposalWorkflow", "create_proposal_activity"),
        ("ValidateProposalWorkflow", "validate_proposal_activity"),
        ("CreateStrategyWorkflow", "create_strategy_activity"),
        ("ValidateStrategyWorkflow", "validate_strategy_activity"),
        ("PromotionDecisionWorkflow", "promotion_decision_activity"),
        ("CommitteeMemoWorkflow", "committee_memo_activity"),
        ("AdvisorStartWorkflow", "advisor_start_activity"),
        ("AdvisorMessageWorkflow", "advisor_message_activity"),
        ("AdvisorCompleteWorkflow", "advisor_complete_activity"),
    ],
)
def test_workflow_delegates_to_its_activity(monkeypatch, workflow_attr, activity_name) -> None:
    from investment_team.temporal import advisory as adv

    sentinel = {"ok": True}
    calls = _patch_execute(monkeypatch, sentinel)

    workflow_cls = getattr(adv, workflow_attr)
    payload = {"id": "x"}
    result = asyncio.run(workflow_cls().run(payload))

    assert result is sentinel
    assert len(calls) == 1
    name, args, kw = calls[0]
    assert name == activity_name
    assert args == [payload]
    # Bounded, non-retrying policy for the interactive execute-and-wait path.
    assert kw["retry_policy"].maximum_attempts == 1
    assert kw["start_to_close_timeout"].total_seconds() == pytest.approx(120)
