"""API tests for the cognition operator router — store layer mocked.

Proves the HTTP surface over the cognition stores: memory inspect (events +
summaries), proposal listing/fetch, approve/reject, rules listing, and the error
mapping (404 unknown, 409 RuleStoreError, 503 storage down, 400 bad filter/scale).
The stores are monkeypatched so no live Postgres is needed; the approve path's
single-activation guarantee lives in the store and is tested there.

Decision provenance (``decided_by``) is server-derived via ``resolve_author`` — the
tests patch it and assert the resolved handle reaches the store, never a caller
body (approve/reject are body-less POSTs).
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


def _event(eid="e1") -> MemoryEvent:
    return MemoryEvent(
        id=eid,
        agent_id="a",
        kind=EventKind.OBSERVATION,
        content="saw a thing",
        salience=0.5,
        occurred_at=_NOW,
        source_run_id="run-1",
        source_seq=0,
    )


def _summary(sid="s1", scale=Scale.DAY) -> PeriodSummary:
    return PeriodSummary(
        id=sid,
        agent_id="a",
        scale=scale,
        period_start=_NOW,
        period_end=_NOW,
        summary="a day",
        created_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Memory — events
# ---------------------------------------------------------------------------
def test_list_memory_events_ok(client, monkeypatch):
    seen = {}

    def _recent(agent_id, top_n, by_salience=True, *, since=None):
        seen.update(agent_id=agent_id, top_n=top_n, by_salience=by_salience, since=since)
        return [_event()]

    monkeypatch.setattr(cognition_mod.memory_store, "fetch_recent_events", _recent)
    resp = client.get(
        "/api/cognition/agents/a/memory/events",
        params={"top_n": 10, "by_salience": "false"},
    )
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "e1"
    assert seen["top_n"] == 10
    assert seen["by_salience"] is False


def test_list_memory_events_forwards_since(client, monkeypatch):
    seen = {}

    def _recent(agent_id, top_n, by_salience=True, *, since=None):
        seen["since"] = since
        return []

    monkeypatch.setattr(cognition_mod.memory_store, "fetch_recent_events", _recent)
    resp = client.get(
        "/api/cognition/agents/a/memory/events",
        params={"since": "2025-01-01T00:00:00Z"},
    )
    assert resp.status_code == 200
    assert seen["since"] == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_list_memory_events_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.memory_store, "fetch_recent_events", _down)
    resp = client.get("/api/cognition/agents/a/memory/events")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Memory — summaries
# ---------------------------------------------------------------------------
def test_list_memory_summaries_ok(client, monkeypatch):
    seen = {}

    def _summaries(agent_id, scale, limit=None, offset=0, *, exclude_stale=False):
        seen.update(scale=scale, limit=limit, offset=offset, exclude_stale=exclude_stale)
        return [_summary()]

    monkeypatch.setattr(cognition_mod.memory_store, "fetch_summaries", _summaries)
    resp = client.get("/api/cognition/agents/a/memory/summaries", params={"scale": "day"})
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "s1"
    assert seen["scale"] == Scale.DAY


def test_list_memory_summaries_forwards_params(client, monkeypatch):
    seen = {}

    def _summaries(agent_id, scale, limit=None, offset=0, *, exclude_stale=False):
        seen.update(limit=limit, offset=offset, exclude_stale=exclude_stale)
        return []

    monkeypatch.setattr(cognition_mod.memory_store, "fetch_summaries", _summaries)
    resp = client.get(
        "/api/cognition/agents/a/memory/summaries",
        params={"scale": "day", "limit": 10, "offset": 5, "exclude_stale": "true"},
    )
    assert resp.status_code == 200
    assert seen == {"limit": 10, "offset": 5, "exclude_stale": True}


def test_list_memory_summaries_bad_scale_is_400(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.memory_store, "fetch_summaries", lambda *a, **k: [])
    resp = client.get("/api/cognition/agents/a/memory/summaries", params={"scale": "decade"})
    assert resp.status_code == 400


def test_list_memory_summaries_missing_scale_is_422(client):
    resp = client.get("/api/cognition/agents/a/memory/summaries")
    assert resp.status_code == 422  # scale is a required query param


def test_list_memory_summaries_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.memory_store, "fetch_summaries", _down)
    resp = client.get("/api/cognition/agents/a/memory/summaries", params={"scale": "week"})
    assert resp.status_code == 503


def test_last_summary_ok(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.memory_store, "get_last_summary", lambda a, s: _summary(scale=s))
    resp = client.get("/api/cognition/agents/a/memory/summaries/last", params={"scale": "month"})
    assert resp.status_code == 200
    assert resp.json()["scale"] == "month"


def test_last_summary_404(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.memory_store, "get_last_summary", lambda a, s: None)
    resp = client.get("/api/cognition/agents/a/memory/summaries/last", params={"scale": "day"})
    assert resp.status_code == 404


def test_last_summary_bad_scale_is_400(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.memory_store, "get_last_summary", lambda a, s: None)
    resp = client.get("/api/cognition/agents/a/memory/summaries/last", params={"scale": "nope"})
    assert resp.status_code == 400


def test_last_summary_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.memory_store, "get_last_summary", _down)
    resp = client.get("/api/cognition/agents/a/memory/summaries/last", params={"scale": "day"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Proposals — list / get
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


def test_get_proposal_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.store, "get_proposal", _down)
    resp = client.get("/api/cognition/agents/a/proposals/p1")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Approve — author from resolve_author, body-less POST
# ---------------------------------------------------------------------------
def test_approve_uses_resolve_author(client, monkeypatch):
    captured = {}

    def _approve(agent_id, proposal_id, *, decided_by, now=None, new_rule_id=None):
        captured.update(agent_id=agent_id, proposal_id=proposal_id, decided_by=decided_by)
        assert now is not None and now.tzinfo is not None  # tz-aware required by the store
        return _rule()

    monkeypatch.setattr(cognition_mod.store, "approve_proposal", _approve)
    monkeypatch.setattr(cognition_mod, "resolve_author", lambda: "opsbot")
    resp = client.post("/api/cognition/agents/a/proposals/p1/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert captured["decided_by"] == "opsbot"  # provenance is server-derived, not caller-supplied


def test_approve_not_pending_is_409(client, monkeypatch):
    def _approve(*a, **k):
        raise RuleStoreError("proposal 'p1' is approved, not pending")

    monkeypatch.setattr(cognition_mod.store, "approve_proposal", _approve)
    resp = client.post("/api/cognition/agents/a/proposals/p1/approve")
    assert resp.status_code == 409
    assert "not pending" in resp.json()["detail"]


def test_approve_unknown_is_404(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "approve_proposal", lambda *a, **k: None)
    resp = client.post("/api/cognition/agents/a/proposals/missing/approve")
    assert resp.status_code == 404


def test_approve_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.store, "approve_proposal", _down)
    resp = client.post("/api/cognition/agents/a/proposals/p1/approve")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Reject — author from resolve_author, body-less POST
# ---------------------------------------------------------------------------
def test_reject_uses_resolve_author(client, monkeypatch):
    captured = {}

    def _reject(agent_id, proposal_id, *, decided_by, now=None):
        captured["decided_by"] = decided_by
        return _proposal(pid=proposal_id, status=ProposalStatus.REJECTED)

    monkeypatch.setattr(cognition_mod.store, "reject_proposal", _reject)
    monkeypatch.setattr(cognition_mod, "resolve_author", lambda: "opsbot")
    resp = client.post("/api/cognition/agents/a/proposals/p1/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert captured["decided_by"] == "opsbot"


def test_reject_unknown_is_404(client, monkeypatch):
    monkeypatch.setattr(cognition_mod.store, "reject_proposal", lambda *a, **k: None)
    resp = client.post("/api/cognition/agents/a/proposals/x/reject")
    assert resp.status_code == 404


def test_reject_not_pending_is_409(client, monkeypatch):
    def _boom(*a, **k):
        raise RuleStoreError("not pending")

    monkeypatch.setattr(cognition_mod.store, "reject_proposal", _boom)
    resp = client.post("/api/cognition/agents/a/proposals/p1/reject")
    assert resp.status_code == 409


def test_reject_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.store, "reject_proposal", _down)
    resp = client.post("/api/cognition/agents/a/proposals/p1/reject")
    assert resp.status_code == 503


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


def test_list_rules_storage_down_is_503(client, monkeypatch):
    def _down(*a, **k):
        raise AgentCognitionStorageUnavailable("no pg")

    monkeypatch.setattr(cognition_mod.store, "list_rules", _down)
    resp = client.get("/api/cognition/agents/a/rules")
    assert resp.status_code == 503
