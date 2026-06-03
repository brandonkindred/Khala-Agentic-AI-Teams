"""Agent Cognition Core — batteries-included memory + rules engine.

This package provides a per-agent cognition substrate (episodic memory,
calendar rollups, advisory/enforced rules, a human-in-the-loop proposal
queue, and an invoke idempotency ledger) that the platform attaches to
generated agents. See ``DESIGN.md`` / ``IMPLEMENTATION_PLAN.md`` in this
directory for the full design and the staged delivery plan.

This module re-exports the domain models for convenience; importing it has
no side effects (the Postgres schema is registered explicitly from the
unified API lifespan via ``register_team_schemas``).
"""

from __future__ import annotations

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
]
