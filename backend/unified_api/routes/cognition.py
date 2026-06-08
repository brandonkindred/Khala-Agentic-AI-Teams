"""Operator approval API for Agent Cognition learned rules (HITL).

Thin HTTP surface over the existing rules store — **no new activation logic**. The
``approve_proposal`` store function remains the single, transactional path that
turns a ``pending`` proposal into an ``active`` rule (it already refuses
not-pending and stale-evidence proposals); these routes only expose it and its
read/reject siblings to an operator. The knowledge graph enriches *proposals*
upstream but never reaches this gate.

Routes (all under ``/api/cognition``):
    GET    /agents/{agent_id}/proposals?status=pending   — list proposals
    GET    /agents/{agent_id}/proposals/{proposal_id}     — one proposal
    POST   /agents/{agent_id}/proposals/{proposal_id}/approve — activate (decided_by)
    POST   /agents/{agent_id}/proposals/{proposal_id}/reject  — reject (decided_by)
    GET    /agents/{agent_id}/rules?status=active          — list rules

Handlers are synchronous ``def`` so FastAPI runs them in its threadpool, keeping
the synchronous psycopg store off the event loop. Errors map cleanly:
``AgentCognitionStorageUnavailable`` → 503, ``RuleStoreError`` → 409, an unknown
proposal → 404, a bad ``status`` filter → 400.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.models import (
    ProposalStatus,
    Rule,
    RuleProposal,
    RuleStatus,
)
from agent_cognition.rules import store
from agent_cognition.rules.store import RuleStoreError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cognition", tags=["cognition"])


class ProposalDecision(BaseModel):
    """Body for approve/reject — who is making the human decision."""

    decided_by: str = Field(..., min_length=1, description="Operator handle/email deciding.")


def _parse_proposal_status(raw: str | None) -> ProposalStatus | None:
    if raw is None:
        return None
    try:
        return ProposalStatus(raw)
    except ValueError as exc:
        valid = ", ".join(s.value for s in ProposalStatus)
        raise HTTPException(
            status_code=400, detail=f"Invalid proposal status {raw!r}; expected one of: {valid}"
        ) from exc


def _parse_rule_status(raw: str | None) -> RuleStatus | None:
    if raw is None:
        return None
    try:
        return RuleStatus(raw)
    except ValueError as exc:
        valid = ", ".join(s.value for s in RuleStatus)
        raise HTTPException(status_code=400, detail=f"Invalid rule status {raw!r}; expected one of: {valid}") from exc


def _storage_guard(exc: AgentCognitionStorageUnavailable) -> HTTPException:
    logger.warning("cognition API: storage unavailable: %s", exc)
    return HTTPException(status_code=503, detail="Agent Cognition storage is unavailable.")


@router.get("/agents/{agent_id}/proposals", response_model=list[RuleProposal])
def list_proposals(
    agent_id: str,
    status: str | None = Query(default=None, description="Filter by proposal status."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[RuleProposal]:
    parsed = _parse_proposal_status(status)
    try:
        return store.list_proposals(agent_id, status=parsed, limit=limit, offset=offset)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_guard(exc) from exc


@router.get("/agents/{agent_id}/proposals/{proposal_id}", response_model=RuleProposal)
def get_proposal(agent_id: str, proposal_id: str) -> RuleProposal:
    try:
        proposal = store.get_proposal(agent_id, proposal_id)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_guard(exc) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    return proposal


@router.post("/agents/{agent_id}/proposals/{proposal_id}/approve", response_model=Rule)
def approve_proposal(agent_id: str, proposal_id: str, decision: ProposalDecision) -> Rule:
    try:
        rule = store.approve_proposal(
            agent_id,
            proposal_id,
            decided_by=decision.decided_by,
            now=datetime.now(timezone.utc),
        )
    except RuleStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_guard(exc) from exc
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    return rule


@router.post("/agents/{agent_id}/proposals/{proposal_id}/reject", response_model=RuleProposal)
def reject_proposal(agent_id: str, proposal_id: str, decision: ProposalDecision) -> RuleProposal:
    try:
        proposal = store.reject_proposal(
            agent_id,
            proposal_id,
            decided_by=decision.decided_by,
            now=datetime.now(timezone.utc),
        )
    except RuleStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_guard(exc) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    return proposal


@router.get("/agents/{agent_id}/rules", response_model=list[Rule])
def list_rules(
    agent_id: str,
    status: str | None = Query(default=None, description="Filter by rule status."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Rule]:
    parsed = _parse_rule_status(status)
    try:
        return store.list_rules(agent_id, status=parsed, limit=limit, offset=offset)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_guard(exc) from exc
