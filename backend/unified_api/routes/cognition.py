"""Operator review/approval API for Agent Cognition (HITL).

Thin HTTP surface over the existing cognition stores — **no new activation
logic**. The ``approve_proposal`` store function remains the single,
transactional path that turns a ``pending`` proposal into an ``active`` rule (it
already refuses not-pending and stale-evidence proposals); these routes only
expose it and its read/reject siblings, plus read-only inspect endpoints over
memory and rules, to an operator. The knowledge graph enriches *proposals*
upstream but never reaches this gate.

The operator identity recorded on a decision (``decided_by``) is **server-derived
provenance** via ``resolve_author`` — a best-effort "who decided" handle, never
supplied by the caller and never an access-control decision (it can be
``anonymous``).

Routes (all under ``/api/cognition``):
    GET    /agents/{agent_id}/memory/events                    — recent memory events
    GET    /agents/{agent_id}/memory/summaries?scale=day       — period summaries at a scale
    GET    /agents/{agent_id}/memory/summaries/last?scale=day  — latest summary at a scale
    GET    /agents/{agent_id}/proposals?status=pending         — list proposals
    GET    /agents/{agent_id}/proposals/{proposal_id}          — one proposal
    POST   /agents/{agent_id}/proposals/{proposal_id}/approve  — activate (author-tagged)
    POST   /agents/{agent_id}/proposals/{proposal_id}/reject   — reject (author-tagged)
    GET    /agents/{agent_id}/rules?status=active              — list rules

Handlers are synchronous ``def`` so FastAPI runs them in its threadpool, keeping
the synchronous psycopg stores off the event loop. Errors map cleanly:
``AgentCognitionStorageUnavailable`` → 503, ``RuleStoreError`` → 409, an unknown
proposal/summary → 404, a bad ``status``/``scale`` value → 400.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, HTTPException, Query

from agent_cognition.memory import store as memory_store
from agent_cognition.memory.store import AgentCognitionStorageUnavailable
from agent_cognition.models import (
    MemoryEvent,
    PeriodSummary,
    ProposalStatus,
    Rule,
    RuleProposal,
    RuleStatus,
    Scale,
)
from agent_cognition.rules import store
from agent_cognition.rules.store import RuleStoreError
from agent_platform.console import resolve_author

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cognition", tags=["cognition"])


def _parse_enum(raw: str | None, enum_cls: type[Enum], label: str) -> Enum | None:
    """Parse ``raw`` into ``enum_cls`` (``None`` passes through), 400 on a bad value."""
    if raw is None:
        return None
    try:
        return enum_cls(raw)
    except ValueError as exc:
        valid = ", ".join(s.value for s in enum_cls)
        raise HTTPException(status_code=400, detail=f"Invalid {label} value {raw!r}; expected one of: {valid}") from exc


def _storage_unavailable_exception(exc: AgentCognitionStorageUnavailable) -> HTTPException:
    """Return (do not raise) the 503 to surface for a storage outage; log it first."""
    logger.warning("cognition API: storage unavailable: %s", exc)
    return HTTPException(status_code=503, detail="Agent Cognition storage is unavailable.")


# ---------------------------------------------------------------------------
# Memory (read-only inspect)
# ---------------------------------------------------------------------------
@router.get("/agents/{agent_id}/memory/events", response_model=list[MemoryEvent])
def list_memory_events(
    agent_id: str,
    top_n: int = Query(default=50, ge=1, le=500),
    by_salience: bool = Query(default=True),
    since: datetime | None = None,
) -> list[MemoryEvent]:
    """List an agent's most relevant recent memory events.

    Preconditions:
        ``agent_id`` is a required path segment (not validated for emptiness here);
        ``1 <= top_n <= 500`` (query-enforced); ``since`` (optional) is an ISO 8601
        datetime string, e.g. ``2025-01-01T00:00:00Z``.
    Postconditions:
        Returns at most ``top_n`` of the agent's events, ordered by salience then
        recency when ``by_salience`` else by recency, optionally limited to events
        occurring at/after ``since``. 503 when storage is unavailable.
    """
    try:
        return memory_store.fetch_recent_events(agent_id, top_n, by_salience, since=since)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc


@router.get("/agents/{agent_id}/memory/summaries/last", response_model=PeriodSummary)
def last_memory_summary(
    agent_id: str,
    scale: str = Query(..., description="Rollup scale: day, week, month, year."),
) -> PeriodSummary:
    """Return the agent's most recent closed summary at ``scale`` (404 if none)."""
    parsed = _parse_enum(scale, Scale, "scale")
    try:
        summary = memory_store.get_last_summary(agent_id, parsed)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc
    if summary is None:
        raise HTTPException(status_code=404, detail=f"No {scale} summary for agent: {agent_id}")
    return summary


@router.get("/agents/{agent_id}/memory/summaries", response_model=list[PeriodSummary])
def list_memory_summaries(
    agent_id: str,
    scale: str = Query(..., description="Rollup scale: day, week, month, year."),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    exclude_stale: bool = Query(default=False),
) -> list[PeriodSummary]:
    """List the agent's summaries at ``scale``, newest period first.

    Preconditions:
        ``scale`` is one of ``day|week|month|year`` (400 otherwise).
    Postconditions:
        Returns the agent's summaries at ``scale`` ordered by ``period_start``
        descending, ``stale`` rows dropped when ``exclude_stale``. 503 when
        storage is unavailable.
    """
    parsed = _parse_enum(scale, Scale, "scale")
    try:
        return memory_store.fetch_summaries(agent_id, parsed, limit=limit, offset=offset, exclude_stale=exclude_stale)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------
@router.get("/agents/{agent_id}/proposals", response_model=list[RuleProposal])
def list_proposals(
    agent_id: str,
    status: str | None = Query(default=None, description="Filter by proposal status."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[RuleProposal]:
    """List an agent's rule proposals, newest first.

    Preconditions:
        ``status`` (when given) is one of ``pending|approved|rejected|superseded``
        (400 otherwise); ``1 <= limit <= 200`` and ``offset >= 0`` (query-enforced).
    Postconditions:
        Returns the agent's proposals, optionally filtered by ``status``, paginated.
        503 when storage is unavailable.
    """
    parsed = _parse_enum(status, ProposalStatus, "proposal")
    try:
        return store.list_proposals(agent_id, status=parsed, limit=limit, offset=offset)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc


@router.get("/agents/{agent_id}/proposals/{proposal_id}", response_model=RuleProposal)
def get_proposal(agent_id: str, proposal_id: str) -> RuleProposal:
    """Return one proposal owned by ``agent_id``.

    Postconditions:
        Returns the proposal identified by the path params; 404 when no such
        proposal exists for the agent; 503 when storage is unavailable.
    """
    try:
        proposal = store.get_proposal(agent_id, proposal_id)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    return proposal


@router.post("/agents/{agent_id}/proposals/{proposal_id}/approve", response_model=Rule)
def approve_proposal(agent_id: str, proposal_id: str) -> Rule:
    """Approve a pending proposal, tagging the decision with the resolved author.

    The decision author (``decided_by``) is server-derived provenance via
    ``resolve_author`` — never a caller-supplied or access-control value.
    """
    try:
        rule = store.approve_proposal(
            agent_id,
            proposal_id,
            decided_by=resolve_author(),
            now=datetime.now(timezone.utc),
        )
    except RuleStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    return rule


@router.post("/agents/{agent_id}/proposals/{proposal_id}/reject", response_model=RuleProposal)
def reject_proposal(agent_id: str, proposal_id: str) -> RuleProposal:
    """Reject a pending proposal, tagging the decision with the resolved author."""
    try:
        proposal = store.reject_proposal(
            agent_id,
            proposal_id,
            decided_by=resolve_author(),
            now=datetime.now(timezone.utc),
        )
    except RuleStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    return proposal


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
@router.get("/agents/{agent_id}/rules", response_model=list[Rule])
def list_rules(
    agent_id: str,
    status: str | None = Query(default=None, description="Filter by rule status."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Rule]:
    """List an agent's rules, highest-priority and newest first.

    Preconditions:
        ``status`` (when given) is one of ``active|retired`` (400 otherwise);
        ``1 <= limit <= 500`` and ``offset >= 0`` (query-enforced).
    Postconditions:
        Returns the agent's rules, optionally filtered by ``status``, paginated.
        503 when storage is unavailable.
    """
    parsed = _parse_enum(status, RuleStatus, "rule")
    try:
        return store.list_rules(agent_id, status=parsed, limit=limit, offset=offset)
    except AgentCognitionStorageUnavailable as exc:
        raise _storage_unavailable_exception(exc) from exc
