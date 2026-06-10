"""Agent Cognition Core — batteries-included memory + rules engine.

This package provides a per-agent cognition substrate (episodic memory,
calendar rollups, advisory/enforced rules, a human-in-the-loop proposal
queue, and an invoke idempotency ledger) that the platform attaches to
generated agents. See ``DESIGN.md`` / ``IMPLEMENTATION_PLAN.md`` in this
directory for the full design and the staged delivery plan.

This module re-exports the domain models only, so importing the package root
stays cheap and dependency-light (pure pydantic — no psycopg / shared_postgres
pulled in). The CognitiveContext facade lives in :mod:`agent_cognition.context`
and is imported from there directly by the few callers that need it (the invoke
proxy, the scheduler), keeping its psycopg + storage/rules import chain off the
package-root path. Importing this module has no side effects.
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
