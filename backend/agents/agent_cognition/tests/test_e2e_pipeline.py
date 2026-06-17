"""End-to-end verification of the Agent Cognition Core learning loop.

This is the terminal proof for the cognition substrate: a single test that
chains the *whole* pipeline through real production entry points and asserts the
load-bearing invariant — **a rule learned from memory and approved by an operator
is visible to the agent's next invocation**:

    seed synthetic events
        → rollup (events → period summaries)
        → reflection (summaries → pending rule proposal)
        → operator approval (pending proposal → active rule)
        → next invoke (active rule surfaces in the injected CognitionContext)

Unlike the per-stage suites (``test_rollup``, ``test_reflection``,
``test_rules_store``, ``test_invoke_gate``), nothing here is stubbed except the
LLM client: the rollup engine, the reflection engine, the rules store, the
idempotency ledger, and the invoke gate all run against live Postgres. The test
adds **no new product code** — it only composes the shipped functions.

It is skipped automatically when ``POSTGRES_HOST`` is unset, using the same
schema-provision + truncate autouse fixture as the other live-Postgres tests.
The LLM is replaced with a deterministic canned client so the loop is hermetic
and free of network calls.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from agent_cognition import invoke_gate
from agent_cognition.memory import rollup
from agent_cognition.memory import store as memory_store
from agent_cognition.models import (
    CognitionContext,
    CognitionWriteback,
    EventKind,
    MemoryEvent,
    ProposalAction,
    ProposalStatus,
    RuleSource,
    RuleStatus,
    Scale,
)
from agent_cognition.postgres import SCHEMA
from agent_cognition.rules import reflection
from agent_cognition.rules import store as rules_store
from llm_service.interface import LLMClient
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

_UTC = timezone.utc

# A closed day well in the past so its day / ISO week / month are all summarizable
# relative to ``_NOW`` (mirrors the dates used in ``test_rollup``).
_EVENT_DAY = datetime(2026, 5, 4, 12, tzinfo=_UTC)
_NOW = datetime(2026, 6, 2, 12, tzinfo=_UTC)

# The behavioural rule reflection is canned to derive from memory.
_DERIVED_RULE_TEXT = "always cite sources before answering"


pg = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres cognition e2e test",
)


# ---------------------------------------------------------------------------
# Fake LLM — deterministic, satisfies both the rollup and reflection callers.
# ---------------------------------------------------------------------------
class CannedLLM(LLMClient):
    """Returns a fixed digest *and* a fixed proposal list.

    Rollup reads ``summary`` / ``highlights``; reflection reads ``proposals``.
    Returning the union from one client lets a single instance back both stages.
    """

    def __init__(self) -> None:
        self.json_calls: list[str] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.json_calls.append(prompt)
        return {
            "summary": "rolled up",
            "highlights": ["repeated answers lacked citations"],
            "proposals": [{"action": "add", "text": _DERIVED_RULE_TEXT}],
        }

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
        objective: str,
        **kwargs: object,
    ) -> str:
        # Reached only when compact_text needs to shrink an over-budget input.
        return "COMPACTED"


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    if not is_postgres_enabled():
        return
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


@pytest.fixture()
def canned(monkeypatch: pytest.MonkeyPatch) -> CannedLLM:
    """Wire one canned client into both LLM-backed stages for the whole test.

    Keeping ``rollup.get_client`` patched also covers the invoke gate's lazy
    rollup catch-up, so no stage ever reaches a live model.
    """
    client = CannedLLM()
    monkeypatch.setattr(rollup, "get_client", lambda key: client)
    monkeypatch.setattr(reflection, "get_client", lambda key: client)
    return client


def _event(agent_id: str, *, seq: int = 1) -> MemoryEvent:
    return MemoryEvent(
        id=str(uuid4()),
        agent_id=agent_id,
        kind=EventKind.OBSERVATION,
        content="answered a question without citing a source",
        occurred_at=_EVENT_DAY,
        source_run_id=str(uuid4()),
        source_seq=seq,
    )


@pg
def test_learning_loop_active_rule_reaches_next_invoke(canned: CannedLLM) -> None:
    """Seed → rollup → reflect → approve → next invoke sees the new active rule."""
    agent_id = f"e2e-{uuid4().hex}"

    # 1. Seed synthetic episodic memory for a closed day.
    memory_store.append_event(agent_id, _event(agent_id, seq=1))
    memory_store.append_event(agent_id, _event(agent_id, seq=2))

    # 2. Rollup — the closed day/week/month are summarized from the raw events.
    rollup_report = rollup.ensure_rollups_current(agent_id, _NOW)
    assert rollup_report.recomputed.get("day") == 1
    day_start = _EVENT_DAY.replace(hour=0, minute=0, second=0, microsecond=0)
    day_summary = memory_store.get_existing_summary(agent_id, Scale.DAY, day_start)
    assert day_summary is not None and day_summary.summary == "rolled up"

    # 3. Reflection — derives a pending proposal from the summary. Nothing active.
    reflect_report = reflection.reflect(agent_id, _NOW)
    assert reflect_report.proposed == 1
    pending = rules_store.list_proposals(agent_id, status=ProposalStatus.PENDING)
    assert len(pending) == 1
    proposal = pending[0]
    assert proposal.action == ProposalAction.ADD
    assert proposal.proposed_rule is not None
    assert proposal.proposed_rule["text"] == _DERIVED_RULE_TEXT
    # Evidence pins the exact summary version reflection reasoned over.
    assert {"summary_id": day_summary.id, "version": day_summary.version} in proposal.evidence
    # HITL gate: reflection never activates on its own.
    assert rules_store.list_rules(agent_id, status=RuleStatus.ACTIVE) == []

    # 4. Operator approval — the proposal becomes a single active derived rule.
    rule = rules_store.approve_proposal(agent_id, proposal.id, decided_by="operator")
    assert rule is not None
    assert rule.status == RuleStatus.ACTIVE
    assert rule.source == RuleSource.DERIVED
    assert rule.text == _DERIVED_RULE_TEXT
    assert [r.id for r in rules_store.list_rules(agent_id, status=RuleStatus.ACTIVE)] == [rule.id]

    # 5. Next invoke — the active rule is injected into the CognitionContext the
    #    runner receives. This is the proof the learned rule changed behaviour.
    seen: dict[str, CognitionContext | None] = {}

    def runner(
        body: Any, ctx: CognitionContext | None
    ) -> tuple[dict[str, Any], CognitionWriteback]:
        seen["ctx"] = ctx
        return {"ok": True}, CognitionWriteback(events=[])

    outcome = asyncio.run(
        invoke_gate.invoke_in_process(agent_id, {"q": "what is the capital of France?"}, runner)
    )
    assert outcome.status_code == 200

    ctx = seen["ctx"]
    assert ctx is not None
    active_ids = [r.id for r in ctx.rules]
    assert rule.id in active_ids, "the approved rule must surface in the next invoke's context"
    assert any(r.text == _DERIVED_RULE_TEXT for r in ctx.rules)

    # The loop ran entirely against the stub — no stage reached a live model.
    assert canned.json_calls, "rollup + reflection must have driven the canned LLM"
