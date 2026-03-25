"""SQLite-backed store for startup advisor conversation state.

Follows the same pattern as ``BrandingConversationStore``: in-memory DB when no
path is supplied (for tests), file-backed with WAL mode for production.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id    TEXT PRIMARY KEY,
    context_json       TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conv_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_messages ON conv_messages(conversation_id);
CREATE TABLE IF NOT EXISTS conv_artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    artifact_type   TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_artifacts ON conv_artifacts(conversation_id);
"""


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
    """SQLite-backed store for startup advisor chat conversations."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        if db_path is None:
            self._file_path: Optional[str] = None
            self._mem_conn: Optional[sqlite3.Connection] = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.executescript(_SCHEMA)
            self._mem_conn.commit()
        else:
            self._file_path = str(db_path)
            self._mem_conn = None
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_file_schema()

    def _init_file_schema(self) -> None:
        conn = sqlite3.connect(self._file_path, timeout=15)  # type: ignore[arg-type]
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            with self._lock:
                self._mem_conn.row_factory = sqlite3.Row
                yield self._mem_conn
                self._mem_conn.commit()
        else:
            conn = sqlite3.connect(self._file_path, check_same_thread=False, timeout=15)  # type: ignore[arg-type]
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def create(self, conversation_id: Optional[str] = None, context: Optional[dict] = None) -> str:
        cid = conversation_id or str(uuid4())
        ctx_json = json.dumps(context or {})
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute(
                "INSERT INTO conversations (conversation_id, context_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (cid, ctx_json, now, now),
            )
        return cid

    def get(self, conversation_id: str) -> Optional[tuple[List[StoredMessage], dict[str, Any]]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT context_json FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            context = json.loads(row[0]) if row[0] else {}
            msg_rows = conn.execute(
                "SELECT role, content, timestamp FROM conv_messages"
                " WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            messages = [StoredMessage(role=r[0], content=r[1], timestamp=r[2]) for r in msg_rows]
        return (messages, context)

    def append_message(self, conversation_id: str, role: str, content: str) -> bool:
        if role not in ("user", "assistant"):
            return False
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()
                is None
            ):
                return False
            conn.execute(
                "INSERT INTO conv_messages (conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, ts),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (ts, conversation_id),
            )
        return True

    def update_context(self, conversation_id: str, context: dict[str, Any]) -> bool:
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            result = conn.execute(
                "UPDATE conversations SET context_json = ?, updated_at = ? WHERE conversation_id = ?",
                (json.dumps(context), ts, conversation_id),
            )
        return result.rowcount > 0

    def add_artifact(
        self, conversation_id: str, artifact_type: str, title: str, payload: dict[str, Any]
    ) -> int:
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            cursor = conn.execute(
                "INSERT INTO conv_artifacts (conversation_id, artifact_type, title, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (conversation_id, artifact_type, title, json.dumps(payload), ts),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_artifacts(self, conversation_id: str) -> List[StoredArtifact]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, artifact_type, title, payload_json, created_at"
                " FROM conv_artifacts WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [
            StoredArtifact(
                artifact_id=r[0],
                artifact_type=r[1],
                title=r[2],
                payload=json.loads(r[3]),
                created_at=r[4],
            )
            for r in rows
        ]

    def list_conversations(self) -> List[ConversationSummary]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT c.conversation_id, c.created_at, c.updated_at, COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN conv_messages m ON m.conversation_id = c.conversation_id
                GROUP BY c.conversation_id, c.created_at, c.updated_at
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
        return [
            ConversationSummary(
                conversation_id=str(r[0]),
                created_at=str(r[1] or ""),
                updated_at=str(r[2] or ""),
                message_count=int(r[3] or 0),
            )
            for r in rows
        ]

    def get_or_create_singleton(self) -> str:
        """Return the single conversation ID, creating one if none exists.

        The startup advisor uses a single persistent conversation per deployment.
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT conversation_id FROM conversations ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is not None:
                return str(row[0])
        return self.create()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_default_store: Optional[StartupAdvisorConversationStore] = None


def get_conversation_store() -> StartupAdvisorConversationStore:
    global _default_store
    if _default_store is None:
        from startup_advisor.db import get_db_path

        _default_store = StartupAdvisorConversationStore(db_path=get_db_path())
    return _default_store
