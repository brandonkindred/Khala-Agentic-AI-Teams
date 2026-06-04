"""Live-Postgres tests for the cognition rules store (Step 5).

Skipped automatically when ``POSTGRES_HOST`` is unset, matching the memory store
tests. The autouse fixture registers the schema and truncates the cognition
tables before each test so cases are independent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agent_cognition.models import (
    ProposalAction,
    ProposalStatus,
    Rule,
    RuleMode,
    RuleProposal,
    RuleSource,
    RuleStatus,
)
from agent_cognition.postgres import SCHEMA
from agent_cognition.rules import store
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres store tests",
)

_UTC = timezone.utc
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _rule(
    agent_id: str,
    *,
    rid: str | None = None,
    text: str = "rule",
    mode: RuleMode = RuleMode.ADVISORY,
    status: RuleStatus = RuleStatus.ACTIVE,
    predicate: dict | None = None,
    rationale: str | None = None,
    source: RuleSource = RuleSource.OPERATOR,
    evidence: list | None = None,
    priority: int = 0,
) -> Rule:
    return Rule(
        id=rid or str(uuid4()),
        agent_id=agent_id,
        text=text,
        mode=mode,
        status=status,
        predicate=predicate or {},
        rationale=rationale,
        source=source,
        evidence=evidence or [],
        needs_review=False,
        priority=priority,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _proposal(
    agent_id: str,
    action: ProposalAction,
    *,
    pid: str | None = None,
    target_rule_id: str | None = None,
    proposed_rule: dict | None = None,
    status: ProposalStatus = ProposalStatus.PENDING,
    evidence: list | None = None,
    stale_evidence: bool = False,
) -> RuleProposal:
    return RuleProposal(
        id=pid or str(uuid4()),
        agent_id=agent_id,
        action=action,
        target_rule_id=target_rule_id,
        proposed_rule=proposed_rule,
        evidence=evidence or [],
        stale_evidence=stale_evidence,
        status=status,
        decided_by=None,
        decided_at=None,
        created_at=_NOW,
    )


def _add_spec(text: str = "new rule", mode: str = "advisory", **extra: object) -> dict:
    spec: dict = {"text": text, "mode": mode}
    spec.update(extra)
    return spec


# ---------------------------------------------------------------------------
# Rules CRUD
# ---------------------------------------------------------------------------
def test_create_rule_round_trip() -> None:
    rule = _rule(
        "a",
        predicate={"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}},
        evidence=[{"summary_id": "s", "version": 1}],
        priority=3,
    )
    store.create_rule("a", rule)
    got = store.get_rule("a", rule.id)
    assert got is not None
    assert got.text == rule.text
    assert got.predicate == rule.predicate
    assert got.evidence == rule.evidence
    assert got.priority == 3


def test_list_rules_filter_and_order() -> None:
    store.create_rule("a", _rule("a", rid="r1", priority=1))
    store.create_rule("a", _rule("a", rid="r2", priority=5))
    store.create_rule("a", _rule("a", rid="r3", status=RuleStatus.RETIRED))
    assert [r.id for r in store.list_rules("a", status=RuleStatus.ACTIVE)] == ["r2", "r1"]
    assert {r.id for r in store.list_rules("a")} == {"r1", "r2", "r3"}
    assert [r.id for r in store.list_rules("a", status=RuleStatus.RETIRED)] == ["r3"]


def test_list_rules_limit_offset() -> None:
    for i in range(3):
        store.create_rule("a", _rule("a", rid=f"r{i}", priority=i))
    assert [r.id for r in store.list_rules("a", limit=1)] == ["r2"]  # highest priority first
    assert [r.id for r in store.list_rules("a", limit=1, offset=1)] == ["r1"]


def test_list_rules_negative_args_assert() -> None:
    with pytest.raises(AssertionError):
        store.list_rules("a", offset=-1)
    with pytest.raises(AssertionError):
        store.list_rules("a", limit=-1)


def test_list_active_enforced_rules() -> None:
    pred = {"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}}
    store.create_rule("a", _rule("a", rid="e1", mode=RuleMode.ENFORCED, predicate=pred, priority=2))
    store.create_rule("a", _rule("a", rid="adv", mode=RuleMode.ADVISORY))
    store.create_rule(
        "a",
        _rule("a", rid="ret", mode=RuleMode.ENFORCED, predicate=pred, status=RuleStatus.RETIRED),
    )
    assert [r.id for r in store.list_active_enforced_rules("a")] == ["e1"]


# ---------------------------------------------------------------------------
# Proposals CRUD
# ---------------------------------------------------------------------------
def test_create_proposal_round_trip() -> None:
    proposal = _proposal(
        "a",
        ProposalAction.ADD,
        proposed_rule=_add_spec(),
        evidence=[{"summary_id": "s", "version": 1}],
    )
    store.create_proposal("a", proposal)
    got = store.get_proposal("a", proposal.id)
    assert got is not None
    assert got.action == ProposalAction.ADD
    assert got.proposed_rule is not None and got.proposed_rule["text"] == "new rule"
    assert got.evidence == [{"summary_id": "s", "version": 1}]


def test_proposal_coherence_asserts() -> None:
    with pytest.raises(AssertionError):
        store.create_proposal("a", _proposal("a", ProposalAction.ADD))  # no proposed_rule
    with pytest.raises(AssertionError):
        store.create_proposal("a", _proposal("a", ProposalAction.RETIRE))  # no target
    with pytest.raises(AssertionError):
        store.create_proposal(
            "a", _proposal("a", ProposalAction.AMEND, target_rule_id="x")
        )  # no proposed_rule


def test_list_proposals_status_filter_incl_superseded() -> None:
    store.create_proposal(
        "a", _proposal("a", ProposalAction.ADD, pid="p1", proposed_rule=_add_spec())
    )
    store.create_proposal(
        "a",
        _proposal(
            "a",
            ProposalAction.ADD,
            pid="p2",
            proposed_rule=_add_spec(),
            status=ProposalStatus.SUPERSEDED,
        ),
    )
    assert [p.id for p in store.list_proposals("a", status=ProposalStatus.PENDING)] == ["p1"]
    assert [p.id for p in store.list_proposals("a", status=ProposalStatus.SUPERSEDED)] == ["p2"]
    assert len(store.list_proposals("a")) == 2


def test_list_proposals_limit_offset() -> None:
    for i in range(3):
        store.create_proposal(
            "a", _proposal("a", ProposalAction.ADD, pid=f"p{i}", proposed_rule=_add_spec())
        )
    # equal created_at → ordered by id ASC
    assert [p.id for p in store.list_proposals("a", limit=2)] == ["p0", "p1"]
    assert [p.id for p in store.list_proposals("a", limit=2, offset=2)] == ["p2"]


# ---------------------------------------------------------------------------
# Approve / reject
# ---------------------------------------------------------------------------
def test_approve_add_inserts_active_rule() -> None:
    proposal = _proposal("a", ProposalAction.ADD, proposed_rule=_add_spec(text="be nice"))
    store.create_proposal("a", proposal)
    rule = store.approve_proposal("a", proposal.id, decided_by="op")
    assert rule is not None and rule.status == RuleStatus.ACTIVE and rule.text == "be nice"
    decided = store.get_proposal("a", proposal.id)
    assert (
        decided is not None
        and decided.status == ProposalStatus.APPROVED
        and decided.decided_by == "op"
    )
    assert store.get_rule("a", rule.id) is not None


def test_approve_retire_retires_target_no_new_rule() -> None:
    store.create_rule("a", _rule("a", rid="t1", status=RuleStatus.ACTIVE))
    proposal = _proposal("a", ProposalAction.RETIRE, target_rule_id="t1")
    store.create_proposal("a", proposal)
    result = store.approve_proposal("a", proposal.id, decided_by="op")
    assert result is not None and result.id == "t1" and result.status == RuleStatus.RETIRED
    assert store.get_rule("a", "t1").status == RuleStatus.RETIRED  # type: ignore[union-attr]
    assert len(store.list_rules("a")) == 1  # nothing inserted


def test_approve_amend_retires_and_inserts_with_lineage() -> None:
    store.create_rule("a", _rule("a", rid="t1", status=RuleStatus.ACTIVE))
    proposal = _proposal(
        "a",
        ProposalAction.AMEND,
        target_rule_id="t1",
        proposed_rule=_add_spec(text="v2", rationale="better"),
    )
    store.create_proposal("a", proposal)
    new_rule = store.approve_proposal("a", proposal.id, decided_by="op", new_rule_id="t2")
    assert (
        new_rule is not None
        and new_rule.id == "t2"
        and new_rule.status == RuleStatus.ACTIVE
        and new_rule.text == "v2"
    )
    assert new_rule.rationale == "amends t1: better"
    assert store.get_rule("a", "t1").status == RuleStatus.RETIRED  # type: ignore[union-attr]
    assert [r.id for r in store.list_rules("a", status=RuleStatus.ACTIVE)] == ["t2"]


def test_approve_enforced_add_invalid_predicate_raises_no_write() -> None:
    proposal = _proposal(
        "a",
        ProposalAction.ADD,
        proposed_rule=_add_spec(mode="enforced", predicate={"phase": "bogus"}),
    )
    store.create_proposal("a", proposal)
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", proposal.id, decided_by="op")
    assert store.get_proposal("a", proposal.id).status == ProposalStatus.PENDING  # type: ignore[union-attr]
    assert store.list_rules("a") == []


def test_approve_enforced_add_valid_predicate_ok() -> None:
    pred = {"phase": "precondition", "check": {"op": "<=", "path": "input.x", "value": 1}}
    proposal = _proposal(
        "a", ProposalAction.ADD, proposed_rule=_add_spec(mode="enforced", predicate=pred)
    )
    store.create_proposal("a", proposal)
    rule = store.approve_proposal("a", proposal.id, decided_by="op")
    assert rule is not None and rule.mode == RuleMode.ENFORCED and rule.predicate == pred


def test_approve_advisory_add_skips_predicate_validation() -> None:
    proposal = _proposal("a", ProposalAction.ADD, proposed_rule=_add_spec(mode="advisory"))
    store.create_proposal("a", proposal)
    rule = store.approve_proposal("a", proposal.id, decided_by="op")
    assert rule is not None and rule.mode == RuleMode.ADVISORY


def test_approve_amend_invalid_predicate_atomic() -> None:
    store.create_rule("a", _rule("a", rid="t1", status=RuleStatus.ACTIVE))
    proposal = _proposal(
        "a",
        ProposalAction.AMEND,
        target_rule_id="t1",
        proposed_rule=_add_spec(mode="enforced", predicate={"phase": "nope"}),
    )
    store.create_proposal("a", proposal)
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", proposal.id, decided_by="op")
    assert (
        store.get_rule("a", "t1").status == RuleStatus.ACTIVE
    )  # target not retired  # type: ignore[union-attr]
    assert store.get_proposal("a", proposal.id).status == ProposalStatus.PENDING  # type: ignore[union-attr]


def test_approve_add_malformed_spec_raises() -> None:
    store.create_proposal(
        "a",
        _proposal(
            "a", ProposalAction.ADD, pid="p1", proposed_rule={"text": "x", "mode": "nonsense"}
        ),
    )
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", "p1", decided_by="op")
    store.create_proposal(
        "a", _proposal("a", ProposalAction.ADD, pid="p2", proposed_rule={"mode": "advisory"})
    )  # missing text
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", "p2", decided_by="op")


def test_reject_sets_status_rejected() -> None:
    proposal = _proposal("a", ProposalAction.ADD, proposed_rule=_add_spec())
    store.create_proposal("a", proposal)
    updated = store.reject_proposal("a", proposal.id, decided_by="op")
    assert (
        updated is not None
        and updated.status == ProposalStatus.REJECTED
        and updated.decided_by == "op"
    )
    assert store.get_proposal("a", proposal.id).status == ProposalStatus.REJECTED  # type: ignore[union-attr]
    assert store.list_rules("a") == []


def test_approve_and_reject_non_pending_raise() -> None:
    proposal = _proposal("a", ProposalAction.ADD, proposed_rule=_add_spec())
    store.create_proposal("a", proposal)
    store.approve_proposal("a", proposal.id, decided_by="op")
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", proposal.id, decided_by="op")
    with pytest.raises(store.RuleStoreError):
        store.reject_proposal("a", proposal.id, decided_by="op")


def test_approve_and_reject_missing_return_none() -> None:
    assert store.approve_proposal("a", "nope", decided_by="op") is None
    assert store.reject_proposal("a", "nope", decided_by="op") is None


def test_approve_stale_evidence_proposal_raises() -> None:
    # A pending proposal whose evidence went stale on a recompute must not be
    # approved (it would activate a rule from outdated summaries); force re-review.
    proposal = _proposal("a", ProposalAction.ADD, proposed_rule=_add_spec(), stale_evidence=True)
    store.create_proposal("a", proposal)
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", proposal.id, decided_by="op")
    assert store.get_proposal("a", proposal.id).status == ProposalStatus.PENDING  # type: ignore[union-attr]
    assert store.list_rules("a") == []  # nothing activated


# ---------------------------------------------------------------------------
# agent_id isolation
# ---------------------------------------------------------------------------
def test_rule_agent_isolation() -> None:
    store.create_rule("a", _rule("a", rid="r1"))
    store.create_rule("b", _rule("b", rid="r2"))
    assert store.get_rule("a", "r2") is None
    assert [r.id for r in store.list_rules("a")] == ["r1"]


def test_proposal_agent_isolation() -> None:
    store.create_proposal(
        "a", _proposal("a", ProposalAction.ADD, pid="p1", proposed_rule=_add_spec())
    )
    store.create_proposal(
        "b", _proposal("b", ProposalAction.ADD, pid="p2", proposed_rule=_add_spec())
    )
    assert store.get_proposal("a", "p2") is None
    assert [p.id for p in store.list_proposals("a")] == ["p1"]


def test_approve_cannot_touch_other_agents_target() -> None:
    store.create_rule("b", _rule("b", rid="bt", status=RuleStatus.ACTIVE))
    proposal = _proposal("a", ProposalAction.RETIRE, target_rule_id="bt")
    store.create_proposal("a", proposal)
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", proposal.id, decided_by="op")
    assert store.get_rule("b", "bt").status == RuleStatus.ACTIVE  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Seed packs
# ---------------------------------------------------------------------------
def test_install_seed_pack_and_idempotent() -> None:
    ids = store.install_seed_pack("a", "default_guardrails")
    assert len(ids) == 1
    rules = store.list_rules("a")
    assert len(rules) == 1 and rules[0].source == RuleSource.SEED
    assert store.install_seed_pack("a", "default_guardrails") == []  # idempotent
    assert len(store.list_rules("a")) == 1


def test_install_unknown_pack_raises() -> None:
    with pytest.raises(store.RuleStoreError):
        store.install_seed_pack("a", "nope")


def test_install_seed_pack_uses_deterministic_id() -> None:
    ids = store.install_seed_pack("a", "default_guardrails")
    assert ids == [store._seed_rule_id("a", "default_guardrails", "no-secrets-in-output")]


def test_install_seed_pack_validates_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cognition.rules.seed_packs import SeedRule

    # An enforced seed with an invalid predicate must be rejected, not installed
    # inert (the install path is otherwise the only create path that ships rules).
    monkeypatch.setattr(
        store,
        "SEED_PACKS",
        {"bad_pack": [SeedRule(key="x", text="t", mode=RuleMode.ENFORCED, predicate={})]},
    )
    with pytest.raises(store.RuleStoreError):
        store.install_seed_pack("a", "bad_pack")
    assert store.list_rules("a") == []  # nothing inserted


# ---------------------------------------------------------------------------
# Validation, lifecycle, and tz guards (review hardening)
# ---------------------------------------------------------------------------
def test_create_rule_validates_enforced_predicate() -> None:
    # An enforced rule with a missing/invalid phase would be silently inert in the
    # enforcement gate (fail open); reject it at the write boundary.
    with pytest.raises(store.RuleStoreError):
        store.create_rule("a", _rule("a", mode=RuleMode.ENFORCED, predicate={}))
    with pytest.raises(store.RuleStoreError):
        store.create_rule("a", _rule("a", mode=RuleMode.ENFORCED, predicate={"phase": "nope"}))
    # Valid enforced predicate and an advisory empty predicate both persist.
    store.create_rule(
        "a",
        _rule(
            "a",
            rid="ok",
            mode=RuleMode.ENFORCED,
            predicate={
                "phase": "precondition",
                "check": {"op": "==", "path": "input.x", "value": 1},
            },
        ),
    )
    store.create_rule("a", _rule("a", rid="adv", mode=RuleMode.ADVISORY))
    assert {r.id for r in store.list_rules("a")} == {"ok", "adv"}


def test_approve_amend_new_rule_id_collision_raises() -> None:
    store.create_rule("a", _rule("a", rid="t1", status=RuleStatus.ACTIVE))
    proposal = _proposal(
        "a", ProposalAction.AMEND, target_rule_id="t1", proposed_rule=_add_spec(text="v2")
    )
    store.create_proposal("a", proposal)
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", proposal.id, decided_by="op", new_rule_id="t1")
    assert (
        store.get_rule("a", "t1").status == RuleStatus.ACTIVE
    )  # not retired  # type: ignore[union-attr]
    assert store.get_proposal("a", proposal.id).status == ProposalStatus.PENDING  # type: ignore[union-attr]


def test_retire_non_active_target_raises() -> None:
    store.create_rule("a", _rule("a", rid="t1", status=RuleStatus.RETIRED))
    proposal = _proposal("a", ProposalAction.RETIRE, target_rule_id="t1")
    store.create_proposal("a", proposal)
    with pytest.raises(store.RuleStoreError):
        store.approve_proposal("a", proposal.id, decided_by="op")
    assert store.get_proposal("a", proposal.id).status == ProposalStatus.PENDING  # type: ignore[union-attr]


def test_naive_now_asserts() -> None:
    naive = datetime(2026, 6, 1, 12, 0)  # no tzinfo
    with pytest.raises(AssertionError):
        store.approve_proposal("a", "p", decided_by="op", now=naive)
    with pytest.raises(AssertionError):
        store.reject_proposal("a", "p", decided_by="op", now=naive)
    with pytest.raises(AssertionError):
        store.install_seed_pack("a", "default_guardrails", now=naive)


# ---------------------------------------------------------------------------
# Preconditions & infra guards
# ---------------------------------------------------------------------------
def test_empty_agent_id_asserts() -> None:
    with pytest.raises(AssertionError):
        store.list_rules("")
    with pytest.raises(AssertionError):
        store.get_rule("", "x")
    with pytest.raises(AssertionError):
        store.list_proposals("")
    with pytest.raises(AssertionError):
        store.install_seed_pack("", "default_guardrails")
    with pytest.raises(AssertionError):
        store.approve_proposal("", "p", decided_by="op")
    with pytest.raises(AssertionError):
        store.approve_proposal("a", "p", decided_by="")
    with pytest.raises(AssertionError):
        store.reject_proposal("a", "p", decided_by="")


def test_storage_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cognition.memory.store import AgentCognitionStorageUnavailable

    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)
    with pytest.raises(AgentCognitionStorageUnavailable):
        store.list_rules("a")


def test_conn_propagates_body_errors_unwrapped() -> None:
    with pytest.raises(ValueError):
        with store._conn() as conn:
            assert conn is not None
            raise ValueError("boom")
