"""Process-wide Agent Studio service singleton.

Every authoring request is served by
:class:`~agent_platform.studio.service.AgentStudioService`, reached directly and
in-process: the route handlers call :func:`get_studio_service` and invoke the service
method synchronously. Authoring CRUD is a request/response RPC, not durable work, so it
does not go through Temporal. All HTTP handlers in a process must share one service
instance — with the in-memory store, that shared instance is what makes a conversation
created on one request resolvable on the next.

The store selection (Postgres when configured, else in-memory) is bound **once at
import time**: ``POSTGRES_HOST`` must be set before this module first loads. Flipping
the env at runtime does not re-select the store.
"""

from __future__ import annotations

import logging

from agent_platform.studio.service import AgentStudioService

logger = logging.getLogger(__name__)


def _build_service() -> AgentStudioService:
    """Build the process-wide service with a durable store when Postgres is on.

    With ``POSTGRES_HOST`` set the conversation store is Postgres-backed so state is
    coherent across uvicorn workers (a conversation created on one worker
    resolves on another; turns serialize via a row lock). Without it — local dev /
    tests — the in-memory store is used.

    The store is stateless with a lazily-opened pool, so this selection does no I/O:
    it only decides *which* store class to instantiate. The ``except`` is narrowed to
    :class:`ImportError` on purpose (``ModuleNotFoundError`` is a subclass, so it's
    already covered) — the only non-connectivity failure possible here is a missing
    optional dependency (e.g. psycopg absent), which legitimately degrades to
    in-memory. A configured-but-unreachable Postgres is deliberately **not**
    downgraded: construction opens no connection, so a connectivity error can only
    surface later inside a request, where it propagates rather than silently forking
    per-worker state. Any other unexpected error at construction likewise propagates
    (fail loud) instead of being swallowed into a silent fallback.

    Postconditions:
        - Returns an :class:`AgentStudioService`; Postgres-backed iff
          ``is_postgres_enabled()`` and the psycopg dependency is importable.
    """
    try:
        from shared.postgres import is_postgres_enabled

        if is_postgres_enabled():
            from agent_platform.studio.pg_store import PostgresAgentStudioConversationStore

            return AgentStudioService(store=PostgresAgentStudioConversationStore())
    except (
        ImportError
    ):  # pragma: no cover - only a missing dep degrades (ModuleNotFoundError is a subclass)
        logger.warning(
            "Postgres Agent Studio store unavailable (missing dependency); using in-memory store",
            exc_info=True,
        )
    return AgentStudioService()


# Process-wide singleton, built eagerly at import to preserve the "store bound once
# at import time" contract.
_service = _build_service()


def get_studio_service() -> AgentStudioService:
    """Return the process-wide Agent Studio service singleton.

    The single instance every request handler in this process shares, so the in-memory
    conversation store is coherent across requests. With Postgres configured the store
    is durable and the singleton is merely a convenience; with the in-memory store it
    is a correctness requirement.

    Coherence is **per process**: dispatch calls this singleton directly within
    whichever process handled the request, so the in-memory store is coherent only in
    single-process mode (``make run``, tests, and default ``make deploy`` / Docker
    ``--workers 1``). A multi-worker deployment **requires** ``POSTGRES_HOST``: without
    it, a follow-up request may be served by a different process whose in-memory store
    lacks the conversation, returning 404.

    Postconditions:
        - Returns the same instance on every call within a process.
    """
    return _service
