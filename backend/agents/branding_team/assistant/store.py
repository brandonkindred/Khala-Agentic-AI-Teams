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
class ConversationState:
    """Full conversation state loaded in a single query.

    Invariants:
        ``messages`` is ordered oldest-first; ``mission`` is always present;
        ``brand_id`` is None for conversations not yet attached to a brand.
    """

    messages: List[_StoredMessage]
    mission: BrandingMission
    latest_output: Optional[TeamOutput]
    brand_id: Optional[str]


@dataclass
class ConversationSummary:
    conversation_id: str
    brand_id: Optional[str]
    created_at: str
    updated_at: str
    message_count: int


def _parse_conversation_rows(
    rows: List[dict],
) -> tuple[BrandingMission, Optional[TeamOutput], List[_StoredMessage]]:
    """Map joined conversation+message rows to (mission, latest_output, messages).

    Shared by :meth:`BrandingConversationStore.get_state` and
    :meth:`BrandingConversationStore.get_by_brand_id`, which run the same
    ``LEFT JOIN`` (differing only in the WHERE key) and parse the result
    identically.

    Preconditions:
        ``rows`` is a non-empty list of dict rows (enforced below); ``rows[0]``
        carries ``mission_json``/``latest_output_json`` as already-deserialized
        dicts (psycopg's ``dict_row`` row factory decodes JSONB columns to
        Python objects, not strings); message rows carry ``role``/``content``/
        ``timestamp`` (``role`` is None for the LEFT-JOIN placeholder when a
        conversation has no messages; ``timestamp`` is ``NOT NULL`` in the
        schema for real message rows, i.e. whenever ``role`` is not None).
    Postconditions:
        Returns the parsed mission, optional latest output, and messages ordered
        as given (oldest-first), skipping the null-role placeholder row.
    """
    assert rows, "_parse_conversation_rows requires at least one row"
    head = rows[0]
    mission = BrandingMission.model_validate(head["mission_json"])
    latest_output = (
        TeamOutput.model_validate(head["latest_output_json"])
        if head["latest_output_json"]
        else None
    )
    messages = [
        _StoredMessage(
            role=r["role"],
            content=r["content"],
            timestamp=_row_ts(r["timestamp"]),
        )
        for r in rows
        if r["role"] is not None
    ]
    return mission, latest_output, messages


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

    @timed_query(store=_STORE, op="get_state")
    def get_state(self, conversation_id: str) -> Optional[ConversationState]:
        """Load a conversation's messages, mission, output, and brand id at once.

        Uses a single ``LEFT JOIN`` so a full conversation load costs one round
        trip instead of the two it previously took (conversation row, then
        messages) plus a third for ``brand_id``.

        Postconditions:
            Returns None when the conversation does not exist, else a fully
            populated :class:`ConversationState` with messages ordered
            oldest-first.

        Note:
            This loads the conversation's *entire* message history in one query
            — the chat endpoints return the full transcript to the client, and
            branding conversations are expected to stay short (a guided 5-phase
            flow). The assistant's ``BRANDING_ASSISTANT_HISTORY_WINDOW`` caps
            only the LLM prompt context, not this load. If conversations ever
            grow large, add a message ``limit`` / pagination here and in the
            response model — tracked as a follow-up.
        """
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT c.brand_id, c.mission_json, c.latest_output_json, "
                "m.role, m.content, m.timestamp "
                "FROM branding_conversations c "
                "LEFT JOIN branding_conv_messages m ON m.conversation_id = c.conversation_id "
                "WHERE c.conversation_id = %s ORDER BY m.id",
                (conversation_id,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        mission, latest_output, messages = _parse_conversation_rows(rows)
        brand_id = str(rows[0]["brand_id"]) if rows[0]["brand_id"] else None
        return ConversationState(
            messages=messages,
            mission=mission,
            latest_output=latest_output,
            brand_id=brand_id,
        )

    def get(
        self, conversation_id: str
    ) -> Optional[tuple[List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        """Backwards-compatible 3-tuple view over :meth:`get_state`."""
        state = self.get_state(conversation_id)
        if state is None:
            return None
        return (state.messages, state.mission, state.latest_output)

    @timed_query(store=_STORE, op="append_message")
    def append_message(self, conversation_id: str, role: str, content: str) -> bool:
        """Append a message and bump ``updated_at`` in a single statement.

        A data-modifying CTE bumps the parent conversation's ``updated_at``
        and only inserts the message when that conversation exists — replacing
        the prior exists-check / insert / touch trio (three round trips, called
        twice per chat turn) with one.

        Postconditions:
            Returns True iff the conversation existed and the message was
            inserted; False for an unknown conversation or invalid role.
        """
        if role not in ("user", "assistant"):
            return False
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "WITH conv AS ("
                "UPDATE branding_conversations SET updated_at = %s "
                "WHERE conversation_id = %s RETURNING conversation_id) "
                "INSERT INTO branding_conv_messages (conversation_id, role, content, timestamp) "
                "SELECT conversation_id, %s, %s, %s FROM conv RETURNING id",
                (ts, conversation_id, role, content, ts),
            )
            return cur.fetchone() is not None

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
        """Return the single conversation for *brand_id*, or None.

        Single ``LEFT JOIN`` load (conversation + messages) — same pattern as
        :meth:`get_state`, keyed by brand id.
        """
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT c.conversation_id, c.mission_json, c.latest_output_json, "
                "m.role, m.content, m.timestamp "
                "FROM branding_conversations c "
                "LEFT JOIN branding_conv_messages m ON m.conversation_id = c.conversation_id "
                "WHERE c.brand_id = %s ORDER BY m.id",
                (brand_id,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        cid = str(rows[0]["conversation_id"])
        mission, latest_output, messages = _parse_conversation_rows(rows)
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
