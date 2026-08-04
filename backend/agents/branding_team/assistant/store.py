"""Postgres-backed store for branding conversation state.

Data is persisted in the shared Khala Postgres instance via
``shared.postgres.get_conn``. DDL lives in ``branding_team.postgres`` and
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

from psycopg.types.json import Json

from shared.postgres import PostgresHelperMixin
from shared.postgres.metrics import timed_query

from ..models import (
    MISSION_PLACEHOLDER_TBD,
    MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
    BrandingMission,
    TeamOutput,
)

logger = logging.getLogger(__name__)

_STORE = "branding_conversations"


def _default_mission() -> BrandingMission:
    """Return a placeholder ``BrandingMission`` when none is stored yet.

    Preconditions:
        None — safe to call with no arguments.
    Postconditions:
        Returns a ``BrandingMission`` whose ``company_name`` and
        ``target_audience`` are ``MISSION_PLACEHOLDER_TBD`` and whose
        ``company_description`` is ``MISSION_PLACEHOLDER_TO_BE_DISCUSSED``.
    """
    return BrandingMission(
        company_name=MISSION_PLACEHOLDER_TBD,
        company_description=MISSION_PLACEHOLDER_TO_BE_DISCUSSED,
        target_audience=MISSION_PLACEHOLDER_TBD,
    )


def _row_ts(value: Any) -> str:
    """Convert a Postgres timestamp cell to a string for API responses.

    Preconditions:
        ``value`` is a ``datetime``, a string, or ``None`` (other types are
        coerced via ``str``). Strings are not validated as ISO timestamps.
    Postconditions:
        Returns ``value.isoformat()`` when ``value`` is a ``datetime``;
        otherwise returns ``str(value)`` unchanged when truthy, or ``""``
        when ``value`` is ``None`` or otherwise falsy (no format check).
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


@dataclass
class _StoredMessage:
    """A single chat message as persisted and loaded from Postgres.

    Invariants:
        ``role`` and ``content`` are strings that may be empty (schema is
        ``TEXT NOT NULL`` only); ``timestamp`` is the string from
        ``_row_ts`` (ISO only when the cell was a ``datetime``; other
        values are preserved/coerced without format validation). Rows with
        ``role is None`` are LEFT-JOIN placeholders and are never
        constructed as ``_StoredMessage``.
    """

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
    """Summary view of a branding conversation for list endpoints.

    Invariants:
        ``message_count`` is >= 0; ``brand_id`` is None when the conversation
        is not attached to a brand; timestamps are ISO-formatted strings.
    """

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
    if not rows:
        raise ValueError("_parse_conversation_rows requires at least one row")
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


class BrandingConversationStore(PostgresHelperMixin):
    """Postgres-backed store for chat conversations and mission state."""

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared.postgres.
        super().__init__()

    @timed_query(store=_STORE, op="create")
    def create(
        self,
        conversation_id: Optional[str] = None,
        brand_id: Optional[str] = None,
        mission: Optional[BrandingMission] = None,
        latest_output: Optional[TeamOutput] = None,
    ) -> str:
        """Insert a new conversation row and return its id.

        Preconditions:
            ``conversation_id`` if provided is unique among existing rows;
            ``mission`` / ``latest_output`` if provided are valid models.
        Postconditions:
            A row is inserted into ``branding_conversations``. Returns the
            provided ``conversation_id`` when it is a non-empty string;
            otherwise generates a new UUID4 (``None`` and ``""`` both count
            as absent). Uses ``_default_mission()`` when ``mission`` is None;
            stores ``latest_output`` as JSONB or NULL.
        """
        cid = conversation_id or str(uuid4())
        m = mission or _default_mission()
        output_dict = latest_output.model_dump(mode="json") if latest_output else None
        now = datetime.now(tz=timezone.utc)
        self._execute(
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
        rows = self._fetch_all(
            "SELECT c.brand_id, c.mission_json, c.latest_output_json, "
            "m.role, m.content, m.timestamp "
            "FROM branding_conversations c "
            "LEFT JOIN branding_conv_messages m ON m.conversation_id = c.conversation_id "
            "WHERE c.conversation_id = %s ORDER BY m.id",
            (conversation_id,),
        )
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
        row = self._fetch_one(
            "WITH conv AS ("
            "UPDATE branding_conversations SET updated_at = %s "
            "WHERE conversation_id = %s RETURNING conversation_id) "
            "INSERT INTO branding_conv_messages (conversation_id, role, content, timestamp) "
            "SELECT conversation_id, %s, %s, %s FROM conv RETURNING id",
            (ts, conversation_id, role, content, ts),
        )
        return row is not None

    @timed_query(store=_STORE, op="update_mission")
    def update_mission(self, conversation_id: str, mission: BrandingMission) -> bool:
        """Replace the mission JSON for a conversation and bump ``updated_at``.

        Preconditions:
            ``conversation_id`` is a non-empty string; ``mission`` is a valid
            :class:`BrandingMission`.
        Postconditions:
            Returns True iff a matching conversation row was updated; False
            when no such conversation exists.
        Raises:
            ValueError: if ``conversation_id`` is empty.
        """
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty string")
        ts = datetime.now(tz=timezone.utc)
        return (
            self._execute(
                "UPDATE branding_conversations SET mission_json = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (Json(mission.model_dump(mode="json")), ts, conversation_id),
            )
            > 0
        )

    @timed_query(store=_STORE, op="update_output")
    def update_output(self, conversation_id: str, output: Optional[TeamOutput]) -> bool:
        """Set or clear the latest team output for a conversation.

        Passing ``output=None`` clears ``latest_output_json``.

        Preconditions:
            ``conversation_id`` is a non-empty string; ``output`` is None or a
            valid :class:`TeamOutput`.
        Postconditions:
            Returns True iff a matching conversation row was updated; False
            when no such conversation exists. ``updated_at`` is bumped on
            success.
        Raises:
            ValueError: if ``conversation_id`` is empty.
        """
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty string")
        output_dict = output.model_dump(mode="json") if output else None
        ts = datetime.now(tz=timezone.utc)
        return (
            self._execute(
                "UPDATE branding_conversations SET latest_output_json = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (
                    Json(output_dict) if output_dict is not None else None,
                    ts,
                    conversation_id,
                ),
            )
            > 0
        )

    @timed_query(store=_STORE, op="set_brand")
    def set_brand(self, conversation_id: str, brand_id: Optional[str]) -> bool:
        """Attach or detach a conversation from a brand.

        Passing ``brand_id=None`` clears the association.

        Preconditions:
            ``conversation_id`` is a non-empty string; ``brand_id`` is None or
            a non-empty brand id string.
        Postconditions:
            Returns True iff a matching conversation row was updated; False
            when no such conversation exists. ``updated_at`` is bumped on
            success.
        Raises:
            ValueError: if ``conversation_id`` is empty, or ``brand_id`` is
                an empty string (``None`` is allowed and clears the brand).
        """
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty string")
        if brand_id is not None and not brand_id:
            raise ValueError("brand_id must be None or a non-empty string")
        ts = datetime.now(tz=timezone.utc)
        return (
            self._execute(
                "UPDATE branding_conversations SET brand_id = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (brand_id, ts, conversation_id),
            )
            > 0
        )

    @timed_query(store=_STORE, op="attach_and_update_mission")
    def attach_and_update_mission(
        self, conversation_id: str, brand_id: Optional[str], mission: BrandingMission
    ) -> bool:
        """Attach a conversation to a brand and replace its mission, atomically.

        Combines what ``set_brand`` and ``update_mission`` would otherwise do as
        two independently-committed statements into one transaction, so a
        conversation can never be left attached to a brand with a stale mission
        (or vice versa) if the second write were to fail.

        Preconditions:
            ``conversation_id`` is a non-empty string; ``brand_id`` is None or a
            non-empty brand id string; ``mission`` is a valid :class:`BrandingMission`.
        Postconditions:
            Returns True iff a matching conversation row was updated with both
            ``brand_id`` and ``mission_json``; False when no such conversation
            exists (the transaction still commits, but affects zero rows).
            ``updated_at`` is bumped once on success.
        Raises:
            ValueError: if ``conversation_id`` is empty, or ``brand_id`` is
                an empty string (``None`` is allowed).
        """
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty string")
        if brand_id is not None and not brand_id:
            raise ValueError("brand_id must be None or a non-empty string")
        ts = datetime.now(tz=timezone.utc)
        with self._transaction() as cur:
            cur.execute(
                "UPDATE branding_conversations "
                "SET brand_id = %s, mission_json = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (brand_id, Json(mission.model_dump(mode="json")), ts, conversation_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="get_by_brand_id")
    def get_by_brand_id(
        self, brand_id: str
    ) -> Optional[tuple[str, List[_StoredMessage], BrandingMission, Optional[TeamOutput]]]:
        """Return the single conversation for *brand_id*, or None.

        Single ``LEFT JOIN`` load (conversation + messages) — same pattern as
        :meth:`get_state`, keyed by brand id. A subquery first pins the one
        canonical conversation for the brand (the most recently updated, per
        the "single conversation per brand" contract), then the join loads
        that conversation's full message history. Filtering the join directly
        by ``brand_id`` (with no per-conversation restriction) would let stray
        extra conversation rows for the same brand have their messages merged
        into one corrupted result.
        """
        rows = self._fetch_all(
            "SELECT c.conversation_id, c.mission_json, c.latest_output_json, "
            "m.role, m.content, m.timestamp "
            "FROM branding_conversations c "
            "LEFT JOIN branding_conv_messages m ON m.conversation_id = c.conversation_id "
            "WHERE c.conversation_id = ("
            "  SELECT conversation_id FROM branding_conversations "
            "  WHERE brand_id = %s ORDER BY updated_at DESC LIMIT 1"
            ") "
            "ORDER BY m.id",
            (brand_id,),
        )
        if not rows:
            return None
        cid = str(rows[0]["conversation_id"])
        mission, latest_output, messages = _parse_conversation_rows(rows)
        return (cid, messages, mission, latest_output)

    @timed_query(store=_STORE, op="list_conversations")
    def list_conversations(self, brand_id: Optional[str] = None) -> List[ConversationSummary]:
        """List conversations, optionally filtered by brand.

        Preconditions:
            ``brand_id`` is None or a non-empty brand id string.
        Postconditions:
            Returns summaries ordered by most recently updated first (empty
            when none match). When ``brand_id`` is set, only conversations
            attached to that brand are included; ``message_count`` is the
            number of rows in ``branding_conv_messages`` for each conversation.
        Raises:
            ValueError: if ``brand_id`` is an empty string (``None`` is
                allowed and means "no filter").
        """
        if brand_id is not None and not brand_id:
            raise ValueError("brand_id must be None or a non-empty string")
        params: list[Any] = []
        where_clause = ""
        if brand_id is not None:
            where_clause = "WHERE c.brand_id = %s"
            params.append(brand_id)
        rows = self._fetch_all(
            f"""
            SELECT c.conversation_id, c.brand_id, c.created_at, c.updated_at,
                   COUNT(m.id) AS message_count
            FROM branding_conversations c
            LEFT JOIN branding_conv_messages m ON m.conversation_id = c.conversation_id
            {where_clause}
            GROUP BY c.conversation_id, c.brand_id, c.created_at, c.updated_at
            ORDER BY c.updated_at DESC
            """,
            params,
        )
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
        """Return the brand id associated with a conversation, if any.

        Preconditions:
            ``conversation_id`` is a non-empty string.
        Postconditions:
            Returns the associated brand id as a string, or None when the
            conversation does not exist or has no brand attached.
        Raises:
            ValueError: if ``conversation_id`` is empty.
        """
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty string")
        row = self._fetch_one(
            "SELECT brand_id FROM branding_conversations WHERE conversation_id = %s",
            (conversation_id,),
        )
        if row is None or not row["brand_id"]:
            return None
        return str(row["brand_id"])


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[BrandingConversationStore] = None


def get_conversation_store() -> BrandingConversationStore:
    global _default_store
    if _default_store is None:
        _default_store = BrandingConversationStore()
    return _default_store
