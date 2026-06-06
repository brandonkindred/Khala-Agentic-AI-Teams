"""Pydantic domain models for the Agent Cognition Core.

These are the shared types that the memory store, rollup engine, rules
engine, tools layer, and invoke proxy (later steps) read and write. They
mirror the Postgres schema in :mod:`agent_cognition.postgres` one-to-one.

Conventions follow the rest of the codebase: Pydantic v2 ``BaseModel``,
``str | None`` for optionals, ``Field(default_factory=...)`` for mutable
defaults, and ``str``-mixed enums so values serialize as plain strings.

Design by Contract:

* Preconditions — callers must supply fields that satisfy the declared
  types and enums; pydantic validation raises on violation at construction.
* Postconditions — a constructed model round-trips through
  ``model_dump()`` / ``model_validate()`` without loss.
* Invariants — enum members exactly match the values persisted in the
  corresponding ``TEXT`` columns (e.g. ``ProposalStatus`` carries the
  system-only terminal state ``superseded``).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums — values match the TEXT columns in agent_cognition.postgres.SCHEMA.
# ---------------------------------------------------------------------------
class EventKind(str, Enum):
    """The category of an episodic memory event."""

    OBSERVATION = "observation"
    ACTION = "action"
    TOOL_CALL = "tool_call"
    OUTCOME = "outcome"
    ERROR = "error"
    FEEDBACK = "feedback"


class Scale(str, Enum):
    """The calendar scale of a rollup summary."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class RuleMode(str, Enum):
    """Whether a rule only steers the prompt or is deterministically enforced."""

    ADVISORY = "advisory"
    ENFORCED = "enforced"


class RuleStatus(str, Enum):
    """Lifecycle status of a rule (see DESIGN §8.3)."""

    ACTIVE = "active"
    RETIRED = "retired"


class RuleSource(str, Enum):
    """Provenance of a rule."""

    SEED = "seed"
    DERIVED = "derived"
    OPERATOR = "operator"


class ProposalAction(str, Enum):
    """The mutation a rule proposal applies on approval."""

    ADD = "add"
    RETIRE = "retire"
    AMEND = "amend"


class ProposalStatus(str, Enum):
    """Lifecycle status of a rule proposal (see DESIGN §8.3).

    ``superseded`` is the *system* auto-withdraw terminal state for a
    proposal whose evidence went stale on recompute — distinct from
    ``rejected``, which records an explicit human decision.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


# ---------------------------------------------------------------------------
# Persistent rows.
# ---------------------------------------------------------------------------
class MemoryEvent(BaseModel):
    """One append-only episodic memory event.

    Invariant: ``(agent_id, source_run_id, source_seq)`` is unique — the
    idempotency key that lets a duplicated writeback be a no-op.
    """

    id: str
    agent_id: str
    kind: EventKind
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    salience: float = 0.0
    occurred_at: datetime
    source_run_id: str
    source_seq: int


class PeriodSummary(BaseModel):
    """A calendar-scoped rollup of memory for one closed period.

    Invariant: ``(agent_id, scale, period_start)`` is unique; ``version``
    increments on every recompute and ``stale`` flags a period for
    re-summarization after a late event lands inside it.
    """

    id: str
    agent_id: str
    scale: Scale
    period_start: datetime
    period_end: datetime
    summary: str = ""
    highlights: list[Any] = Field(default_factory=list)
    source_count: int = 0
    covers_through: datetime | None = None
    version: int = 1
    stale: bool = False
    # Store-managed retention regime marker: set TRUE by the memory store's
    # prune_events when a day's raw events are deleted, so a reader that
    # rediscovers a stale summary knows it must be *amended* (regime b) rather
    # than recomputed from the now-incomplete raw events (regime a). Read-only
    # for callers — upsert_summary never writes it, so a recompute cannot clear
    # it. Defaults to FALSE.
    events_pruned: bool = False
    created_at: datetime


class Rule(BaseModel):
    """An advisory or enforced guardrail for an agent.

    ``evidence`` holds ``(summary_id, version)`` refs for ``derived`` rules
    so a later recompute can flag ``needs_review`` when the evidence moves.
    """

    id: str
    agent_id: str
    text: str
    mode: RuleMode
    status: RuleStatus
    predicate: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = None
    source: RuleSource
    evidence: list[Any] = Field(default_factory=list)
    needs_review: bool = False
    priority: int = 0
    created_at: datetime
    updated_at: datetime


class RuleProposal(BaseModel):
    """A human-in-the-loop proposal to add/retire/amend a rule.

    The explicit ``action`` + ``target_rule_id`` let the approve path apply
    retire/amend deterministically instead of mis-activating them as new
    rules.
    """

    id: str
    agent_id: str
    action: ProposalAction
    target_rule_id: str | None = None
    proposed_rule: dict[str, Any] | None = None
    evidence: list[Any] = Field(default_factory=list)
    stale_evidence: bool = False
    status: ProposalStatus = ProposalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Invoke-path envelope payloads (not persisted rows; carried on the wire).
# ---------------------------------------------------------------------------
class ToolCall(BaseModel):
    """A single tool invocation recorded in a writeback / shim audit.

    ``tool_id`` is the *declared* tool (e.g. ``git``) the enforced rules and
    manifest reference; ``function`` is the specific advertised operation that
    actually ran (e.g. ``git_status``), preserved so the out-of-band audit can
    distinguish operations of a multi-function tool, not just the umbrella id.
    """

    tool_id: str
    function: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result: Any = None
    error: str | None = None
    occurred_at: datetime | None = None


class CognitionContext(BaseModel):
    """The ``cognition`` side-channel block injected on invoke.

    Carries the advisory rules the agent runtime should render into its
    system prompt and the compact memory digest. Kept out of the agent's
    user-input contract (see DESIGN §10 envelope).
    """

    rules: list[Rule] = Field(default_factory=list)
    memory_digest: str = ""


class CognitionWriteback(BaseModel):
    """The cognition payload an agent returns alongside its output.

    ``truncated`` flags that ``events`` / ``tool_calls`` were trimmed to fit
    the writeback byte cap rather than failing the whole call.
    """

    events: list[MemoryEvent] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    truncated: bool = False
