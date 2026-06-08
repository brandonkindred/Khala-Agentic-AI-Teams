"""API tests for the cognition approval router — store layer mocked.

Proves the HTTP surface over the rules store: listing, fetch, approve/reject, and
the error mapping (404 unknown, 409 RuleStoreError, 503 storage down, 400 bad
filter). The store itself is monkeypatched so no live Postgres is needed; the
approve path's single-activation guarantee lives in the store and is tested there.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.models import (
    ProposalAction,
    ProposalStatus,
    Rule,
    RuleMode,
    RuleProposal,
    RuleSource,
    RuleStatus,
)
from agent_cognition.rules.store import RuleStoreError
from unified_api.routes import cognition as cognition_mod
from unified_api.routes.cognition import router

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _proposal(pid="p1", status=ProposalStatus.PENDING) -> RuleProposal:
    return RuleProposal(
        id=pid,
        agent_id="a",
        action=ProposalAction.ADD,
        proposed_rule={"text": "be careful", "mode": "advisory", "source": "derived"},
        status=status,
        created_at=_NOW,
    )


def _rule(rid="r1") -> Rule:
    return Rule(
        id=rid,
        agent_id="a",
        text="be careful",
        mode=RuleMode.ADVISORY,
        status=RuleStatus.ACTIVE,
        source=RuleSource.DERIVED,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------
def test_list_proposals(client, monkeypatch):
    seen = {}

    def _list(agent_id, status=None, limit=None, offset=0):
        seen.update(agent_id=agent_id, status=status, limit=limit, offset=offset)
        return [_proposal()]

    monkeypatch.setattr(cognition_mod.store, "list_proposals", _list)
    resp = client.get("/api/cognition/agents/a/proposals", params={"status": "pending"})
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "p1"
    assert seen["status"] == ProposalStatus.PENDING


def test_list_proposals_bad_status_is_400(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "list_proposals", lambda *a, **k: [])
    resp = client.get("/api/cognition/agents/a/proposals", params={"status": "bogus"})
    assert resp.status_code == 400


def test_list_proposals_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.store, "list_proposals", _down)
    resp = client.get("/api/cognition/agents/a/proposals")
    assert resp.status_code == 503


def test_get_proposal_404(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "get_proposal", lambda a, p: None)
    resp = client.get("/api/cognition/agents/a/proposals/missing")
    assert resp.status_code == 404


def test_get_proposal_ok(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "get_proposal", lambda a, p: _proposal(pid=p))
    resp = client.get("/api/cognition/agents/a/proposals/p9")
    assert resp.status_code == 200
    assert resp.json()["id"] == "p9"


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------
def test_approve_activates_rule(client, monkeypatch):
    captured = {}

    def _approve(agent_id, proposal_id, *, decided_by, now=None, new_rule_id=None):
        captured.update(agent_id=agent_id, proposal_id=proposal_id, decided_by=decided_by)
        assert now is not None and now.tzinfo is not None  # tz-aware required by the store
        return _rule()

    monkeypatch.setattr(cognition_mod.store, "approve_proposal", _approve)
    resp = client.post("/api/cognition/agents/a/proposals/p1/approve", json={"decided_by": "ops@x.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert captured["decided_by"] == "ops@x.com"


def test_approve_requires_decided_by(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "approve_proposal", lambda *a, **k: _rule())
    resp = client.post("/api/cognition/agents/a/proposals/p1/approve", json={})
    assert resp.status_code == 422  # pydantic validation


def test_approve_not_pending_is_409(client, monkeypatch):
    def _approve(*a, **k):
        raise RuleStoreError("proposal 'p1' is approved, not pending")

    monkeypatch.setattr(cognition_mod.store, "approve_proposal", _approve)
    resp = client.post("/api/cognition/agents/a/proposals/p1/approve", json={"decided_by": "ops"})
    assert resp.status_code == 409
    assert "not pending" in resp.json()["detail"]


def test_approve_unknown_is_404(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "approve_proposal", lambda *a, **k: None)
    resp = client.post("/api/cognition/agents/a/proposals/missing/approve", json={"decided_by": "ops"})
    assert resp.status_code == 404


def test_approve_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.store, "approve_proposal", _down)
    resp = client.post("/api/cognition/agents/a/proposals/p1/approve", json={"decided_by": "ops"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------
def test_reject_ok(client, monkeypatch):
    monkeypatch.setattr(
        cognition_mod.store,
        "reject_proposal",
        lambda a, p, *, decided_by, now=None: _proposal(pid=p, status=ProposalStatus.REJECTED),
    )
    resp = client.post("/api/cognition/agents/a/proposals/p1/reject", json={"decided_by": "ops"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_reject_unknown_is_404(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "reject_proposal", lambda *a, **k: None)
    resp = client.post("/api/cognition/agents/a/proposals/x/reject", json={"decided_by": "ops"})
    assert resp.status_code == 404


def test_reject_not_pending_is_409(client, monkeypatch):
    def _boom(*a, **k):
        raise RuleStoreError("not pending")

    monkeypatch.setattr(cognition_mod.store, "reject_proposal", _boom)
    resp = client.post("/api/cognition/agents/a/proposals/p1/reject", json={"decided_by": "ops"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def test_list_rules(client, monkeypatch):
    seen = {}

    def _list(agent_id, status=None, limit=None, offset=0):
        seen["status"] = status
        return [_rule()]

    monkeypatch.setattr(cognition_mod.store, "list_rules", _list)
    resp = client.get("/api/cognition/agents/a/rules", params={"status": "active"})
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "r1"
    assert seen["status"] == RuleStatus.ACTIVE


def test_list_rules_bad_status_400(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "list_rules", lambda *a, **k: [])
    resp = client.get("/api/cognition/agents/a/rules", params={"status": "weird"})
    assert resp.status_code == 400
