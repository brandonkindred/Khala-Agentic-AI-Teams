"""Postgres-backed conversation store for team assistants.

Conversations are partitioned by ``team_key`` so every assistant sub-app
has its own isolated namespace in the shared Khala Postgres instance.
DDL lives in ``team_assistant.postgres`` and is registered from the
unified_api lifespan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "team_assistant"


def _row_ts(value: Any) -> str:
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


class TeamAssistantConversationStore:
    """Postgres-backed store for team assistant conversations.

    ``team_key`` scopes every query so multiple assistants mounted by
    the unified API don't see each other's conversations.
    """

    def __init__(self, team_key: str) -> None:
        if not team_key:
            raise ValueError("team_key is required")
        self._team_key = team_key

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create")
    def create(
        self,
        conversation_id: Optional[str] = None,
        context: Optional[dict] = None,
        job_id: Optional[str] = None,
    ) -> str:
        cid = conversation_id or str(uuid4())
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO team_assistant_conversations "
                "(conversation_id, team_key, job_id, context_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (cid, self._team_key, job_id, Json(context or {}), now, now),
            )
        return cid

    @timed_query(store=_STORE, op="get")
    def get(self, conversation_id: str) -> Optional[tuple[List[StoredMessage], dict[str, Any]]]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT context_json FROM team_assistant_conversations "
                "WHERE conversation_id = %s AND team_key = %s",
                (conversation_id, self._team_key),
            )
            row = cur.fetchone()
            if row is None:
                return None
            context = row["context_json"] or {}
            cur.execute(
                "SELECT role, content, timestamp FROM team_assistant_conv_messages "
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
                "SELECT 1 FROM team_assistant_conversations "
                "WHERE conversation_id = %s AND team_key = %s",
                (conversation_id, self._team_key),
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                "INSERT INTO team_assistant_conv_messages "
                "(conversation_id, role, content, timestamp) VALUES (%s, %s, %s, %s)",
                (conversation_id, role, content, ts),
            )
            cur.execute(
                "UPDATE team_assistant_conversations SET updated_at = %s "
                "WHERE conversation_id = %s AND team_key = %s",
                (ts, conversation_id, self._team_key),
            )
        return True

    @timed_query(store=_STORE, op="update_context")
    def update_context(self, conversation_id: str, context: dict[str, Any]) -> bool:
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE team_assistant_conversations "
                "SET context_json = %s, updated_at = %s "
                "WHERE conversation_id = %s AND team_key = %s",
                (Json(context), ts, conversation_id, self._team_key),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="add_artifact")
    def add_artifact(
        self, conversation_id: str, artifact_type: str, title: str, payload: dict[str, Any]
    ) -> int:
        """Attach an artifact to ``conversation_id``.

        Returns the new artifact's ``id``, or ``0`` when the conversation
        either does not exist or belongs to a different ``team_key``. The
        existence check is scoped by ``(conversation_id, team_key)`` so a
        stale or leaked ID from another team cannot mutate this team's
        data — matching the defensive pattern used by ``append_message``.
        """
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM team_assistant_conversations "
                "WHERE conversation_id = %s AND team_key = %s",
                (conversation_id, self._team_key),
            )
            if cur.fetchone() is None:
                logger.warning(
                    "add_artifact: conversation_id=%s not owned by team_key=%s; dropping artifact",
                    conversation_id,
                    self._team_key,
                )
                return 0
            cur.execute(
                "INSERT INTO team_assistant_conv_artifacts "
                "(conversation_id, artifact_type, title, payload_json, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (conversation_id, artifact_type, title, Json(payload), ts),
            )
            row = cur.fetchone()
            return int(row[0])

    @timed_query(store=_STORE, op="get_artifacts")
    def get_artifacts(self, conversation_id: str) -> List[StoredArtifact]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Scope via a JOIN so we only return artifacts for conversations
            # owned by this team.
            cur.execute(
                "SELECT a.id, a.artifact_type, a.title, a.payload_json, a.created_at "
                "FROM team_assistant_conv_artifacts a "
                "JOIN team_assistant_conversations c "
                "  ON c.conversation_id = a.conversation_id "
                "WHERE a.conversation_id = %s AND c.team_key = %s "
                "ORDER BY a.id",
                (conversation_id, self._team_key),
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

    # ------------------------------------------------------------------
    # Job linking
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="link_job")
    def link_job(self, conversation_id: str, job_id: str) -> None:
        """Associate a conversation with a pipeline job."""
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE team_assistant_conversations "
                "SET job_id = %s, updated_at = %s "
                "WHERE conversation_id = %s AND team_key = %s",
                (job_id, ts, conversation_id, self._team_key),
            )

    @timed_query(store=_STORE, op="get_by_job_id")
    def get_by_job_id(self, job_id: str) -> Optional[str]:
        """Return the conversation_id linked to *job_id*, or None."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM team_assistant_conversations "
                "WHERE job_id = %s AND team_key = %s LIMIT 1",
                (job_id, self._team_key),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="list_conversations")
    def list_conversations(self) -> List[Dict[str, Any]]:
        """Return all conversations for this team as dicts."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT conversation_id, job_id, context_json, created_at "
                "FROM team_assistant_conversations "
                "WHERE team_key = %s ORDER BY created_at DESC",
                (self._team_key,),
            )
            rows = cur.fetchall()
        return [
            {
                "conversation_id": str(r["conversation_id"]),
                "job_id": r["job_id"],
                "context": r["context_json"] or {},
                "created_at": _row_ts(r["created_at"]),
            }
            for r in rows
        ]

    @timed_query(store=_STORE, op="list_unlinked")
    def list_unlinked(self) -> List[Dict[str, Any]]:
        """Return conversations with no job_id (drafts)."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT conversation_id, context_json, created_at "
                "FROM team_assistant_conversations "
                "WHERE team_key = %s AND job_id IS NULL ORDER BY created_at DESC",
                (self._team_key,),
            )
            rows = cur.fetchall()
        return [
            {
                "conversation_id": str(r["conversation_id"]),
                "context": r["context_json"] or {},
                "created_at": _row_ts(r["created_at"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="delete_conversation")
    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation and all its messages/artifacts.

        Returns True if the conversation existed and belonged to this team.
        """
        with get_conn() as conn, conn.cursor() as cur:
            # Delete scoped to this team_key: we first check ownership via
            # a DELETE ... RETURNING so the message/artifact cascade only
            # runs when we actually owned the conversation.
            cur.execute(
                "DELETE FROM team_assistant_conversations "
                "WHERE conversation_id = %s AND team_key = %s RETURNING conversation_id",
                (conversation_id, self._team_key),
            )
            deleted = cur.fetchone()
            if not deleted:
                return False
            cur.execute(
                "DELETE FROM team_assistant_conv_artifacts WHERE conversation_id = %s",
                (conversation_id,),
            )
            cur.execute(
                "DELETE FROM team_assistant_conv_messages WHERE conversation_id = %s",
                (conversation_id,),
            )
        return True

    # ------------------------------------------------------------------
    # Legacy singleton (backward compat for non-blogging teams)
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="get_or_create_singleton")
    def get_or_create_singleton(self) -> str:
        """Return the single conversation ID for this team, creating one if none exists."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM team_assistant_conversations "
                "WHERE team_key = %s ORDER BY created_at ASC LIMIT 1",
                (self._team_key,),
            )
            row = cur.fetchone()
            if row is not None:
                return str(row[0])
        return self.create()

    @timed_query(store=_STORE, op="reset_singleton")
    def reset_singleton(self) -> str:
        """Delete every conversation for this team and create a fresh one."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM team_assistant_conversations WHERE team_key = %s",
                (self._team_key,),
            )
            existing_ids = [str(r[0]) for r in cur.fetchall()]
            if existing_ids:
                cur.execute(
                    "DELETE FROM team_assistant_conv_artifacts WHERE conversation_id = ANY(%s)",
                    (existing_ids,),
                )
                cur.execute(
                    "DELETE FROM team_assistant_conv_messages WHERE conversation_id = ANY(%s)",
                    (existing_ids,),
                )
                cur.execute(
                    "DELETE FROM team_assistant_conversations WHERE team_key = %s",
                    (self._team_key,),
                )
        return self.create()


# ---------------------------------------------------------------------------
# Per-team store registry
# ---------------------------------------------------------------------------

_stores: dict[str, TeamAssistantConversationStore] = {}


def get_store(team_key: str) -> TeamAssistantConversationStore:
    """Return the singleton store for a team, creating it on first access."""
    store = _stores.get(team_key)
    if store is None:
        store = TeamAssistantConversationStore(team_key=team_key)
        _stores[team_key] = store
    return store
