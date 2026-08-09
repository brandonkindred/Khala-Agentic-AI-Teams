"""Process-wide Agent Studio drafts store singleton.

Store selection (Postgres when configured, else in-memory) is bound once at
import time — the same contract as ``agent_studio.runtime`` for conversations.
"""

from __future__ import annotations

import logging
from typing import Union

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore

logger = logging.getLogger(__name__)

DraftStore = Union[AgentStudioDraftStore, "PostgresAgentStudioDraftStore"]  # noqa: F821


def _build_draft_store() -> DraftStore:
    """Select Postgres drafts store when enabled, else in-memory.

    Postconditions:
        * Returns a drafts store instance; Postgres-backed iff
          ``is_postgres_enabled()`` and psycopg is importable.
    """
    try:
        from shared.postgres import is_postgres_enabled

        if is_postgres_enabled():
            from agent_team_studio.agent_studio.drafts_pg_store import (
                PostgresAgentStudioDraftStore,
            )

            return PostgresAgentStudioDraftStore()
    except ImportError:  # pragma: no cover - missing optional dep
        logger.warning(
            "Postgres Agent Studio drafts store unavailable (missing dependency); "
            "using in-memory store",
            exc_info=True,
        )
    return AgentStudioDraftStore()


_store = _build_draft_store()


def get_draft_store() -> DraftStore:
    """Return the process-wide drafts store singleton.

    Postconditions:
        * Returns the same instance on every call within a process.
    """
    return _store
