"""
Agent Console — Phase 3 data layer.

Postgres-backed storage for user-saved inputs and run history plus a small
diff helper. Consumed by the unified API routes
``backend/unified_api/routes/agent_console_saved_inputs.py``,
``routes/agent_console_diff.py``, and the invoke / history endpoints in
``routes/agents.py``.

No FastAPI app of its own — this module runs in-process inside the unified
API (same pattern as ``agent_registry``). The team's Postgres schema lives
in :mod:`agent_console.postgres` and is registered from the unified API
lifespan via :func:`shared_postgres.register_team_schemas`.
"""

from .author import resolve_author
from .diff import unified_json_diff
from .models import (
    DiffRequest,
    DiffResult,
    DiffSide,
    RunRecord,
    RunSummary,
    SavedInput,
)
from .store import AgentConsoleStorageUnavailable, AgentConsoleStore, get_store

__all__ = [
    "AgentConsoleStorageUnavailable",
    "AgentConsoleStore",
    "DiffRequest",
    "DiffResult",
    "DiffSide",
    "RunRecord",
    "RunSummary",
    "SavedInput",
    "get_store",
    "resolve_author",
    "unified_json_diff",
]
