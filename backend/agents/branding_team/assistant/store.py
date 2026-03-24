"""SQLite-backed store for branding conversation state.

``BrandingConversationStore()`` with no arguments uses an isolated in-memory
database (one per instance) so unit tests stay independent without any setup.
The production singleton (``get_conversation_store()``) passes a file path so
all worker processes share the same on-disk database.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional
from uuid import uuid4

from branding_team.models import BrandingMission, TeamOutput

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id    TEXT PRIMARY KEY,
    brand_id           TEXT,
    mission_json       TEXT NOT NULL,
    latest_output_json TEXT,
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
"""


def _default_mission() -> BrandingMission:
    return BrandingMission(
        company_name="TBD",
        company_description="To be discussed.",
        target_audience="TBD",
    )


@dataclass
class _StoredMessage:
    role: str
    content: str
    timestamp: str


@dataclass
class ConversationSummary:
    conversation_id: str
    brand_id: Optional[str]
    created_at: str
    updated_at: str
    message_count: int


class BrandingConversationStore:
    """SQLite-backed store for chat conversations and mission state."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        if db_path is None:
            self._file_path: Optional[str] = None
            self._mem_conn: Optional[sqlite3.Connection] = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.executescript(_SCHEMA)
            self._run_migrations(self._mem_conn)
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
        self._run_migrations(conn)
        conn.commit()
        conn.close()

    @staticmethod
    def _run_migrations(conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if "brand_id" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN brand_id TEXT")
        if "created_at" not in cols:
            ts = datetime.now(tz=timezone.utc).isoformat()
            conn.execute("ALTER TABLE conversations ADD COLUMN created_at TEXT")
            conn.execute("UPDATE conversations SET created_at = ? WHERE created_at IS NULL", (ts,))
        if "updated_at" not in cols:
            ts = datetime.now(tz=timezone.utc).isoformat()
            conn.execute("ALTER TABLE conversations ADD COLUMN updated_at TEXT")
            conn.execute("UPDATE conversations SET updated_at = ? WHERE updated_at IS NULL", (ts,))

        # Enforce one conversation per brand.  For brands with multiple conversations,
        # keep the one with the most messages and detach the rest.
        dupes = conn.execute(
            "SELECT brand_id, COUNT(*) FROM conversations WHERE brand_id IS NOT NULL GROUP BY brand_id HAVING COUNT(*) > 1"
        ).fetchall()
        for brand_id, _ in dupes:
            rows = conn.execute(
                "SELECT c.conversation_id, COUNT(m.id) AS cnt"
                " FROM conversations c LEFT JOIN conv_messages m ON m.conversation_id = c.conversation_id"
                " WHERE c.brand_id = ? GROUP BY c.conversation_id ORDER BY cnt DESC",
                (brand_id,),
            ).fetchall()
            # Keep the first (most messages), detach the rest
            for row in rows[1:]:
                conn.execute(
                    "UPDATE conversations SET brand_id = NULL WHERE conversation_id = ?",
                    (row[0],),
                )
                logger.info("Detached duplicate conversation %s from brand %s", row[0], brand_id)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_brand_unique ON conversations(brand_id)"
            " WHERE brand_id IS NOT NULL"
        )

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

    def create(
        self,
        conversation_id: Optional[str] = None,
        brand_id: Optional[str] = None,
        mission: Optional[BrandingMission] = None,
        latest_output: Optional[TeamOutput] = None,
    ) -> str:
        cid = conversation_id or str(uuid4())
        m = mission or _default_mission()
        output_json = latest_output.model_dump_json() if latest_output else None
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute(
                "INSERT INTO conversations (conversation_id, brand_id, mission_json, latest_output_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (cid, brand_id, m.model_dump_json(), output_json, now, now),
            )
        return cid

    def get(
        self, conversation_id: str
    ) -> Optional[tuple[List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT mission_json, latest_output_json FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            mission = BrandingMission.model_validate_json(row[0])
            latest_output = TeamOutput.model_validate_json(row[1]) if row[1] else None
            msg_rows = conn.execute(
                "SELECT role, content, timestamp FROM conv_messages"
                " WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            messages = [_StoredMessage(role=r[0], content=r[1], timestamp=r[2]) for r in msg_rows]
        return (messages, mission, latest_output)

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

    def update_mission(self, conversation_id: str, mission: BrandingMission) -> bool:
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            result = conn.execute(
                "UPDATE conversations SET mission_json = ?, updated_at = ? WHERE conversation_id = ?",
                (mission.model_dump_json(), ts, conversation_id),
            )
        return result.rowcount > 0

    def update_output(self, conversation_id: str, output: Optional[TeamOutput]) -> bool:
        output_json = output.model_dump_json() if output else None
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            result = conn.execute(
                "UPDATE conversations SET latest_output_json = ?, updated_at = ? WHERE conversation_id = ?",
                (output_json, ts, conversation_id),
            )
        return result.rowcount > 0

    def set_brand(self, conversation_id: str, brand_id: Optional[str]) -> bool:
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            result = conn.execute(
                "UPDATE conversations SET brand_id = ?, updated_at = ? WHERE conversation_id = ?",
                (brand_id, ts, conversation_id),
            )
        return result.rowcount > 0

    def get_by_brand_id(
        self, brand_id: str
    ) -> Optional[tuple[str, List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        """Return the single conversation for *brand_id*, or None.

        Returns ``(conversation_id, messages, mission, latest_output)``.
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT conversation_id, mission_json, latest_output_json FROM conversations WHERE brand_id = ?",
                (brand_id,),
            ).fetchone()
            if row is None:
                return None
            cid = str(row[0])
            mission = BrandingMission.model_validate_json(row[1])
            latest_output = TeamOutput.model_validate_json(row[2]) if row[2] else None
            msg_rows = conn.execute(
                "SELECT role, content, timestamp FROM conv_messages WHERE conversation_id = ? ORDER BY id",
                (cid,),
            ).fetchall()
            messages = [_StoredMessage(role=r[0], content=r[1], timestamp=r[2]) for r in msg_rows]
        return (cid, messages, mission, latest_output)

    def list_conversations(self, brand_id: Optional[str] = None) -> List[ConversationSummary]:
        with self._db() as conn:
            if brand_id:
                rows = conn.execute(
                    """
                    SELECT c.conversation_id, c.brand_id, c.created_at, c.updated_at, COUNT(m.id) AS message_count
                    FROM conversations c
                    LEFT JOIN conv_messages m ON m.conversation_id = c.conversation_id
                    WHERE c.brand_id = ?
                    GROUP BY c.conversation_id, c.brand_id, c.created_at, c.updated_at
                    ORDER BY c.updated_at DESC
                    """,
                    (brand_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT c.conversation_id, c.brand_id, c.created_at, c.updated_at, COUNT(m.id) AS message_count
                    FROM conversations c
                    LEFT JOIN conv_messages m ON m.conversation_id = c.conversation_id
                    GROUP BY c.conversation_id, c.brand_id, c.created_at, c.updated_at
                    ORDER BY c.updated_at DESC
                    """
                ).fetchall()
        return [
            ConversationSummary(
                conversation_id=str(r[0]),
                brand_id=(str(r[1]) if r[1] else None),
                created_at=str(r[2] or ""),
                updated_at=str(r[3] or ""),
                message_count=int(r[4] or 0),
            )
            for r in rows
        ]

    def get_conversation_brand_id(self, conversation_id: str) -> Optional[str]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT brand_id FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return str(row[0])


# ---------------------------------------------------------------------------
# Singleton — file-backed for production, shared across all worker processes.
# ---------------------------------------------------------------------------

_default_store: Optional[BrandingConversationStore] = None


def get_conversation_store() -> BrandingConversationStore:
    global _default_store
    if _default_store is None:
        from branding_team.db import get_db_path

        _default_store = BrandingConversationStore(db_path=get_db_path())
    return _default_store
