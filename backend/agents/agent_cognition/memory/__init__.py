"""Episodic memory subsystem for the Agent Cognition Core.

This package owns the durable read/write path for an agent's episodic
events and calendar-scoped rollups. :mod:`agent_cognition.memory.store` is
the data-access layer over the ``agent_cognition_events`` and
``agent_cognition_summaries`` tables; :mod:`agent_cognition.memory.rollup`
builds the calendar summarization engine on top of it, and
:mod:`agent_cognition.memory.retrieval` reads the summaries back into the
compact digest injected on invoke.

Importing this package has no side effects (the Postgres schema is
registered explicitly from the unified API lifespan).
"""

from __future__ import annotations

from agent_cognition.memory.retrieval import build_memory_digest
from agent_cognition.memory.store import (
    AgentCognitionStorageUnavailable,
    append_event,
    fetch_events_for_period,
    fetch_recent_events,
    fetch_recent_unfolded_events,
    fetch_stale_summaries,
    fetch_summaries,
    fetch_summaries_in_window,
    fetch_unfolded_events,
    flag_rules_needing_review,
    flag_stale_proposals,
    get_existing_summary,
    get_last_summary,
    mark_period_stale,
    prune_events,
    upsert_summary,
)

__all__ = [
    "AgentCognitionStorageUnavailable",
    "append_event",
    "build_memory_digest",
    "fetch_events_for_period",
    "fetch_recent_events",
    "fetch_recent_unfolded_events",
    "fetch_stale_summaries",
    "fetch_summaries",
    "fetch_summaries_in_window",
    "fetch_unfolded_events",
    "flag_rules_needing_review",
    "flag_stale_proposals",
    "get_existing_summary",
    "get_last_summary",
    "mark_period_stale",
    "prune_events",
    "upsert_summary",
]
