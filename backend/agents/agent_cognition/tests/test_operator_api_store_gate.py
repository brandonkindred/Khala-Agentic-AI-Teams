"""Route-over-store-gate tests for the operator approval REST API.

These exercise the ``/api/cognition`` proposal routes (`backend/unified_api/routes/
cognition.py`) against the **real** rules store and a live Postgres — the gate that
turns a ``pending`` proposal into an ``active`` rule. The sibling
``test_cognition_route.py`` under ``unified_api/tests/`` mocks the store to assert the
HTTP error mapping fast; this module instead drives the same routes end-to-end and
asserts the resulting rule rows, covering the issue's acceptance:

    approve activates exactly one rule and flips the proposal to ``approved``;
    a stale-evidence proposal is refused; reject changes no rule rows.

It lives in the ``agent_cognition`` suite (not ``unified_api/tests/``) because that is
the suite CI runs against a live Postgres service; like the other live-Postgres tests
here it skips automatically when ``POSTGRES_HOST`` is unset and truncates the cognition
tables before each case so cases are independent.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

# Put backend/ on sys.path so ``unified_api.routes.cognition`` resolves when this
# module is collected from the backend/agents working dir. ``unified_api`` and its
# ``routes`` package have import-light __init__s, so this has no side effects.
_BACKEND = Path(__file__).resolve().parents[3]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unified_api.routes import cognition as cognition_mod  # noqa: E402
from unified_api.routes.cognition import router  # noqa: E402

from agent_cognition.models import (  # noqa: E402
    ProposalAction,
    ProposalStatus,
    Rule,
    RuleMode,
    RuleProposal,
    RuleSource,
    RuleStatus,
)
from agent_cognition.postgres import SCHEMA  # noqa: E402
from agent_cognition.rules import store  # noqa: E402
from shared_postgres import is_postgres_enabled, register_team_schemas  # noqa: E402
from shared_postgres.testing import truncate_team_tables  # noqa: E402

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres route-gate tests",
)

_UTC = timezone.utc
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)
_AGENT = "agent-1"


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Mount only the cognition router (no security gateway) and pin the author.

    ``decided_by`` is server-derived via ``resolve_author``; pinning it to a known
    value makes the recorded operator assertable without depending on the host's
    author profile.
    """
    monkeypatch.setattr(cognition_mod, "resolve_author", lambda: "operator")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Builders (mirror test_rules_store.py; kept local to avoid cross-test imports)
# ---------------------------------------------------------------------------
def _rule(
    agent_id: str = _AGENT,
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
    agent_id: str = _AGENT,
    action: ProposalAction = ProposalAction.ADD,
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


def _approve(client: TestClient, pid: str, agent: str = _AGENT):
    return client.post(f"/api/cognition/agents/{agent}/proposals/{pid}/approve")


def _reject(client: TestClient, pid: str, agent: str = _AGENT):
    return client.post(f"/api/cognition/agents/{agent}/proposals/{pid}/reject")


# ---------------------------------------------------------------------------
# Acceptance: approve / stale-evidence refusal / reject
# ---------------------------------------------------------------------------
def test_approve_activates_exactly_one_rule(client: TestClient) -> None:
    proposal = _proposal(proposed_rule=_add_spec(text="be nice"))
    store.create_proposal(_AGENT, proposal)
    assert store.list_rules(_AGENT, status=RuleStatus.ACTIVE) == []  # nothing active yet

    resp = _approve(client, proposal.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["text"] == "be nice"

    active = store.list_rules(_AGENT, status=RuleStatus.ACTIVE)
    assert len(active) == 1  # exactly one rule activated
    assert active[0].id == body["id"]

    decided = store.get_proposal(_AGENT, proposal.id)
    assert decided is not None
    assert decided.status == ProposalStatus.APPROVED
    assert decided.decided_by == "operator"  # server-derived author recorded


def test_approve_stale_evidence_is_refused(client: TestClient) -> None:
    proposal = _proposal(proposed_rule=_add_spec(), stale_evidence=True)
    store.create_proposal(_AGENT, proposal)

    resp = _approve(client, proposal.id)
    assert resp.status_code == 409  # RuleStoreError → 409
    assert "stale" in resp.json()["detail"].lower()

    # The gate refused without mutating: no rule activated, proposal still pending.
    assert store.list_rules(_AGENT, status=RuleStatus.ACTIVE) == []
    still = store.get_proposal(_AGENT, proposal.id)
    assert still is not None and still.status == ProposalStatus.PENDING


def test_reject_changes_no_rule_rows(client: TestClient) -> None:
    store.create_rule(_AGENT, _rule(rid="r1", text="existing"))
    proposal = _proposal(proposed_rule=_add_spec(text="declined"))
    store.create_proposal(_AGENT, proposal)
    before = {(r.id, r.status) for r in store.list_rules(_AGENT)}

    resp = _reject(client, proposal.id)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    after = {(r.id, r.status) for r in store.list_rules(_AGENT)}
    assert after == before  # reject touched no rule rows
    decided = store.get_proposal(_AGENT, proposal.id)
    assert decided is not None
    assert decided.status == ProposalStatus.REJECTED
    assert decided.decided_by == "operator"


# ---------------------------------------------------------------------------
# Read round-trips + sole-activation-path over HTTP
# ---------------------------------------------------------------------------
def test_list_proposals_and_rules_round_trip(client: TestClient) -> None:
    store.create_proposal(_AGENT, _proposal(pid="p1", proposed_rule=_add_spec(text="one")))
    store.create_proposal(
        _AGENT,
        _proposal(pid="p2", proposed_rule=_add_spec(text="two"), status=ProposalStatus.SUPERSEDED),
    )
    store.create_rule(_AGENT, _rule(rid="ra", text="active rule"))
    store.create_rule(_AGENT, _rule(rid="rr", status=RuleStatus.RETIRED))

    pend = client.get(f"/api/cognition/agents/{_AGENT}/proposals", params={"status": "pending"})
    assert pend.status_code == 200
    assert [p["id"] for p in pend.json()] == ["p1"]

    rules = client.get(f"/api/cognition/agents/{_AGENT}/rules", params={"status": "active"})
    assert rules.status_code == 200
    rows = rules.json()
    assert [r["id"] for r in rows] == ["ra"]
    assert rows[0]["text"] == "active rule"


def test_approve_is_the_only_path_that_activates(client: TestClient) -> None:
    rejected = _proposal(pid="p-rej", proposed_rule=_add_spec(text="rej"))
    approved = _proposal(pid="p-app", proposed_rule=_add_spec(text="app"))
    store.create_proposal(_AGENT, rejected)
    store.create_proposal(_AGENT, approved)

    # Rejecting never activates a rule.
    assert _reject(client, "p-rej").status_code == 200
    assert store.list_rules(_AGENT, status=RuleStatus.ACTIVE) == []

    # Approving is what flips the count to one.
    assert _approve(client, "p-app").status_code == 200
    assert len(store.list_rules(_AGENT, status=RuleStatus.ACTIVE)) == 1


# ---------------------------------------------------------------------------
# Retire / amend branches through the route
# ---------------------------------------------------------------------------
def test_approve_retire_retires_target_no_new_rule(client: TestClient) -> None:
    store.create_rule(_AGENT, _rule(rid="t1", status=RuleStatus.ACTIVE))
    proposal = _proposal(action=ProposalAction.RETIRE, target_rule_id="t1")
    store.create_proposal(_AGENT, proposal)

    resp = _approve(client, proposal.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "t1" and body["status"] == "retired"

    assert store.list_rules(_AGENT, status=RuleStatus.ACTIVE) == []  # target retired
    assert len(store.list_rules(_AGENT)) == 1  # nothing inserted


def test_approve_amend_retires_and_inserts(client: TestClient) -> None:
    store.create_rule(_AGENT, _rule(rid="t1", text="v1", status=RuleStatus.ACTIVE))
    proposal = _proposal(
        action=ProposalAction.AMEND,
        target_rule_id="t1",
        proposed_rule=_add_spec(text="v2"),
    )
    store.create_proposal(_AGENT, proposal)

    resp = _approve(client, proposal.id)
    assert resp.status_code == 200
    new_rule = resp.json()
    assert new_rule["text"] == "v2" and new_rule["status"] == "active"

    active = store.list_rules(_AGENT, status=RuleStatus.ACTIVE)
    assert [r.id for r in active] == [new_rule["id"]]  # only the replacement is active
    assert store.get_rule(_AGENT, "t1").status == RuleStatus.RETIRED  # type: ignore[union-attr]
