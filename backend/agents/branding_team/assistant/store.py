"""Postgres-backed store for branding conversation state.

Data is persisted in the shared Khala Postgres instance via
``shared_postgres.get_conn``. DDL lives in ``branding_team.postgres`` and
is registered from the team's FastAPI lifespan.

The unique-per-brand conversation invariant is enforced by a unique
partial index declared in the schema (``idx_branding_conv_brand_unique``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from branding_team.models import BrandingMission, TeamOutput
from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "branding_conversations"


def _default_mission() -> BrandingMission:
    return BrandingMission(
        company_name="TBD",
        company_description="To be discussed.",
        target_audience="TBD",
    )


def _row_ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


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
    """Postgres-backed store for chat conversations and mission state."""

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    @timed_query(store=_STORE, op="create")
    def create(
        self,
        conversation_id: Optional[str] = None,
        brand_id: Optional[str] = None,
        mission: Optional[BrandingMission] = None,
        latest_output: Optional[TeamOutput] = None,
    ) -> str:
        cid = conversation_id or str(uuid4())
        m = mission or _default_mission()
        output_dict = latest_output.model_dump(mode="json") if latest_output else None
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO branding_conversations "
                "(conversation_id, brand_id, mission_json, latest_output_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    cid,
                    brand_id,
                    Json(m.model_dump(mode="json")),
                    Json(output_dict) if output_dict is not None else None,
                    now,
                    now,
                ),
            )
        return cid

    @timed_query(store=_STORE, op="get")
    def get(
        self, conversation_id: str
    ) -> Optional[tuple[List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT mission_json, latest_output_json FROM branding_conversations "
                "WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            mission = BrandingMission.model_validate(row["mission_json"])
            latest_output = (
                TeamOutput.model_validate(row["latest_output_json"])
                if row["latest_output_json"]
                else None
            )
            cur.execute(
                "SELECT role, content, timestamp FROM branding_conv_messages "
                "WHERE conversation_id = %s ORDER BY id",
                (conversation_id,),
            )
            messages = [
                _StoredMessage(
                    role=r["role"],
                    content=r["content"],
                    timestamp=_row_ts(r["timestamp"]),
                )
                for r in cur.fetchall()
            ]
        return (messages, mission, latest_output)

    @timed_query(store=_STORE, op="append_message")
    def append_message(self, conversation_id: str, role: str, content: str) -> bool:
        if role not in ("user", "assistant"):
            return False
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM branding_conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                "INSERT INTO branding_conv_messages "
                "(conversation_id, role, content, timestamp) VALUES (%s, %s, %s, %s)",
                (conversation_id, role, content, ts),
            )
            cur.execute(
                "UPDATE branding_conversations SET updated_at = %s WHERE conversation_id = %s",
                (ts, conversation_id),
            )
        return True

    @timed_query(store=_STORE, op="update_mission")
    def update_mission(self, conversation_id: str, mission: BrandingMission) -> bool:
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE branding_conversations SET mission_json = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (Json(mission.model_dump(mode="json")), ts, conversation_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="update_output")
    def update_output(self, conversation_id: str, output: Optional[TeamOutput]) -> bool:
        output_dict = output.model_dump(mode="json") if output else None
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE branding_conversations SET latest_output_json = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (
                    Json(output_dict) if output_dict is not None else None,
                    ts,
                    conversation_id,
                ),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="set_brand")
    def set_brand(self, conversation_id: str, brand_id: Optional[str]) -> bool:
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE branding_conversations SET brand_id = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (brand_id, ts, conversation_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="get_by_brand_id")
    def get_by_brand_id(
        self, brand_id: str
    ) -> Optional[tuple[str, List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        """Return the single conversation for *brand_id*, or None."""
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT conversation_id, mission_json, latest_output_json "
                "FROM branding_conversations WHERE brand_id = %s",
                (brand_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cid = str(row["conversation_id"])
            mission = BrandingMission.model_validate(row["mission_json"])
            latest_output = (
                TeamOutput.model_validate(row["latest_output_json"])
                if row["latest_output_json"]
                else None
            )
            cur.execute(
                "SELECT role, content, timestamp FROM branding_conv_messages "
                "WHERE conversation_id = %s ORDER BY id",
                (cid,),
            )
            messages = [
                _StoredMessage(
                    role=r["role"],
                    content=r["content"],
                    timestamp=_row_ts(r["timestamp"]),
                )
                for r in cur.fetchall()
            ]
        return (cid, messages, mission, latest_output)

    @timed_query(store=_STORE, op="list_conversations")
    def list_conversations(self, brand_id: Optional[str] = None) -> List[ConversationSummary]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            if brand_id:
                cur.execute(
                    """
                    SELECT c.conversation_id, c.brand_id, c.created_at, c.updated_at,
                           COUNT(m.id) AS message_count
                    FROM branding_conversations c
                    LEFT JOIN branding_conv_messages m ON m.conversation_id = c.conversation_id
                    WHERE c.brand_id = %s
                    GROUP BY c.conversation_id, c.brand_id, c.created_at, c.updated_at
                    ORDER BY c.updated_at DESC
                    """,
                    (brand_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT c.conversation_id, c.brand_id, c.created_at, c.updated_at,
                           COUNT(m.id) AS message_count
                    FROM branding_conversations c
                    LEFT JOIN branding_conv_messages m ON m.conversation_id = c.conversation_id
                    GROUP BY c.conversation_id, c.brand_id, c.created_at, c.updated_at
                    ORDER BY c.updated_at DESC
                    """
                )
            rows = cur.fetchall()
        return [
            ConversationSummary(
                conversation_id=str(r["conversation_id"]),
                brand_id=(str(r["brand_id"]) if r["brand_id"] else None),
                created_at=_row_ts(r["created_at"]),
                updated_at=_row_ts(r["updated_at"]),
                message_count=int(r["message_count"] or 0),
            )
            for r in rows
        ]

    @timed_query(store=_STORE, op="get_conversation_brand_id")
    def get_conversation_brand_id(self, conversation_id: str) -> Optional[str]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT brand_id FROM branding_conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
        if row is None or not row[0]:
            return None
        return str(row[0])


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[BrandingConversationStore] = None


def get_conversation_store() -> BrandingConversationStore:
    global _default_store
    if _default_store is None:
        _default_store = BrandingConversationStore()
    return _default_store
