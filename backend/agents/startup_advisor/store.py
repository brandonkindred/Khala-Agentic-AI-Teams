"""Postgres-backed store for startup advisor conversation state.

Rewritten in PR 2 of the SQLite → Postgres migration. The public API
(constructor, method names, return types) is identical to the prior
SQLite version so callers in ``startup_advisor/api/main.py`` and the
assistant agent need no changes.

All DDL lives in ``startup_advisor.postgres`` and is registered from
the team's FastAPI lifespan via ``shared_postgres.register_team_schemas``.
This module is pure data access: it opens short-lived connections
through ``shared_postgres.get_conn`` (which is pool-backed since PR 0).

Every public method is wrapped in ``@timed_query`` so slow reads and
writes surface as structured log lines without requiring a Prometheus
exporter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "startup_advisor"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _row_ts(value: Any) -> str:
    """Normalize a Postgres TIMESTAMPTZ value to an ISO-8601 string.

    psycopg3 returns a timezone-aware ``datetime``; the public dataclass
    contract on ``StoredMessage`` / ``StoredArtifact`` / ``ConversationSummary``
    exposes timestamps as strings, so we format here rather than
    forcing every caller to handle both shapes.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


@dataclass
class StoredMessage:
    role: str
    content: str
    timestamp: str


@dataclass
class StoredArtifact:
    artifact_id: int
    artifact_type: str
    title: str
    payload: dict[str, Any]
    created_at: str


@dataclass
class ConversationSummary:
    conversation_id: str
    created_at: str
    updated_at: str
    message_count: int


class StartupAdvisorConversationStore:
    """Postgres-backed store for startup advisor chat conversations.

    The constructor takes no arguments — the Postgres DSN is read from
    the ``POSTGRES_*`` env vars by ``shared_postgres.get_conn``. The
    lazy ``get_conversation_store()`` module-level accessor defers
    instantiation so ``import startup_advisor.store`` stays cheap and
    never touches Postgres on import.
    """

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    @timed_query(store=_STORE, op="create")
    def create(self, conversation_id: Optional[str] = None, context: Optional[dict] = None) -> str:
        cid = conversation_id or str(uuid4())
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO startup_advisor_conversations "
                "(conversation_id, context_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (cid, Json(context or {}), now, now),
            )
        return cid

    @timed_query(store=_STORE, op="get")
    def get(self, conversation_id: str) -> Optional[tuple[list[StoredMessage], dict[str, Any]]]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT context_json FROM startup_advisor_conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            context = row["context_json"] or {}

            cur.execute(
                "SELECT role, content, timestamp FROM startup_advisor_conv_messages "
                "WHERE conversation_id = %s ORDER BY id",
                (conversation_id,),
            )
            messages = [
                StoredMessage(
                    role=r["role"],
                    content=r["content"],
                    timestamp=_row_ts(r["timestamp"]),
                )
                for r in cur.fetchall()
            ]
        return (messages, context)

    @timed_query(store=_STORE, op="append_message")
    def append_message(self, conversation_id: str, role: str, content: str) -> bool:
        if role not in ("user", "assistant"):
            return False
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM startup_advisor_conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                "INSERT INTO startup_advisor_conv_messages "
                "(conversation_id, role, content, timestamp) VALUES (%s, %s, %s, %s)",
                (conversation_id, role, content, ts),
            )
            cur.execute(
                "UPDATE startup_advisor_conversations SET updated_at = %s "
                "WHERE conversation_id = %s",
                (ts, conversation_id),
            )
        return True

    @timed_query(store=_STORE, op="update_context")
    def update_context(self, conversation_id: str, context: dict[str, Any]) -> bool:
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE startup_advisor_conversations "
                "SET context_json = %s, updated_at = %s WHERE conversation_id = %s",
                (Json(context), ts, conversation_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="add_artifact")
    def add_artifact(
        self,
        conversation_id: str,
        artifact_type: str,
        title: str,
        payload: dict[str, Any],
    ) -> int:
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO startup_advisor_conv_artifacts "
                "(conversation_id, artifact_type, title, payload_json, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (conversation_id, artifact_type, title, Json(payload), ts),
            )
            row = cur.fetchone()
            return int(row[0])

    @timed_query(store=_STORE, op="get_artifacts")
    def get_artifacts(self, conversation_id: str) -> list[StoredArtifact]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, artifact_type, title, payload_json, created_at "
                "FROM startup_advisor_conv_artifacts "
                "WHERE conversation_id = %s ORDER BY id",
                (conversation_id,),
            )
            return [
                StoredArtifact(
                    artifact_id=int(r["id"]),
                    artifact_type=r["artifact_type"],
                    title=r["title"],
                    payload=r["payload_json"] or {},
                    created_at=_row_ts(r["created_at"]),
                )
                for r in cur.fetchall()
            ]

    @timed_query(store=_STORE, op="list_conversations")
    def list_conversations(self) -> list[ConversationSummary]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT c.conversation_id, c.created_at, c.updated_at,
                       COUNT(m.id) AS message_count
                FROM startup_advisor_conversations c
                LEFT JOIN startup_advisor_conv_messages m
                    ON m.conversation_id = c.conversation_id
                GROUP BY c.conversation_id, c.created_at, c.updated_at
                ORDER BY c.updated_at DESC
                """
            )
            return [
                ConversationSummary(
                    conversation_id=str(r["conversation_id"]),
                    created_at=_row_ts(r["created_at"]),
                    updated_at=_row_ts(r["updated_at"]),
                    message_count=int(r["message_count"] or 0),
                )
                for r in cur.fetchall()
            ]

    @timed_query(store=_STORE, op="get_or_create_singleton")
    def get_or_create_singleton(self) -> str:
        """Return the single conversation ID, creating one if none exists.

        The startup advisor uses a single persistent conversation per
        deployment. In Postgres this is a straight
        ``ORDER BY created_at ASC LIMIT 1`` — ties are extremely unlikely
        and the worst case is returning the older row, which is fine.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM startup_advisor_conversations "
                "ORDER BY created_at ASC LIMIT 1"
            )
            row = cur.fetchone()
            if row is not None:
                return str(row[0])
        return self.create()


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[StartupAdvisorConversationStore] = None


def get_conversation_store() -> StartupAdvisorConversationStore:
    """Return the process-wide store, instantiating on first call.

    The store itself holds no state — the singleton exists purely to
    give callers a stable identity for caching/mocking in tests.
    """
    global _default_store
    if _default_store is None:
        _default_store = StartupAdvisorConversationStore()
    return _default_store
