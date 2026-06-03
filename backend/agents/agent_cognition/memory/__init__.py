"""Episodic memory subsystem for the Agent Cognition Core.

This package owns the durable read/write path for an agent's episodic
events and calendar-scoped rollups. :mod:`agent_cognition.memory.store` is
the data-access layer over the ``agent_cognition_events`` and
``agent_cognition_summaries`` tables; later steps add the rollup engine and
the retrieval/digest builder on top of it.

Importing this package has no side effects (the Postgres schema is
registered explicitly from the unified API lifespan).
"""

from __future__ import annotations

from agent_cognition.memory.store import (
    AgentCognitionStorageUnavailable,
    append_event,
    fetch_events_for_period,
    fetch_recent_events,
    fetch_summaries,
    get_last_summary,
    mark_period_stale,
    prune_events,
    upsert_summary,
)

__all__ = [
    "AgentCognitionStorageUnavailable",
    "append_event",
    "fetch_events_for_period",
    "fetch_recent_events",
    "fetch_summaries",
    "get_last_summary",
    "mark_period_stale",
    "prune_events",
    "upsert_summary",
]
