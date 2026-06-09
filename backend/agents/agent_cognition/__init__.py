"""Agent Cognition Core — batteries-included memory + rules engine.

This package provides a per-agent cognition substrate (episodic memory,
calendar rollups, advisory/enforced rules, a human-in-the-loop proposal
queue, and an invoke idempotency ledger) that the platform attaches to
generated agents. See ``DESIGN.md`` / ``IMPLEMENTATION_PLAN.md`` in this
directory for the full design and the staged delivery plan.

This module re-exports the domain models and the CognitiveContext facade for
convenience. Importing it has no side effects (the Postgres schema is registered
explicitly from the unified API lifespan via ``register_team_schemas``, and the
facade only opens a connection when its functions are called).
"""

from __future__ import annotations

from agent_cognition.context import (
    ClaimResult,
    ClaimState,
    CognitionBlocked,
    PostconditionViolation,
    PreconditionBlocked,
    claim_run,
    complete_run,
    default_run_lease,
    enforce_postcondition,
    enforce_precondition,
    ensure_rollups_current,
    load_context,
    persist_writeback,
    replay_run,
)
from agent_cognition.models import (
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

__all__ = [
    # Domain models
    "CognitionContext",
    "CognitionWriteback",
    "EventKind",
    "MemoryEvent",
    "PeriodSummary",
    "ProposalAction",
    "ProposalStatus",
    "Rule",
    "RuleMode",
    "RuleProposal",
    "RuleSource",
    "RuleStatus",
    "Scale",
    "ToolCall",
    # CognitiveContext facade (Step 8)
    "ClaimResult",
    "ClaimState",
    "CognitionBlocked",
    "PreconditionBlocked",
    "PostconditionViolation",
    "claim_run",
    "complete_run",
    "default_run_lease",
    "enforce_postcondition",
    "enforce_precondition",
    "ensure_rollups_current",
    "load_context",
    "persist_writeback",
    "replay_run",
]
