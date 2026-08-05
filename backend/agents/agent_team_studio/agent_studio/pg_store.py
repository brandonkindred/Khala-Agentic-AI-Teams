"""Postgres-backed Agent Studio authoring-conversation store.

Durable, cross-worker twin of the in-memory
:class:`~agent_team_studio.agent_studio.store.AgentStudioConversationStore`. Same public interface
(``create`` / ``get`` / ``append_message`` / ``discard`` / ``__len__`` / ``turn``)
so :class:`~agent_team_studio.agent_studio.service.AgentStudioService` is agnostic to which one it
holds; the route module selects this one when ``POSTGRES_HOST`` is set.

Multi-worker coherence (the whole point): all uvicorn workers share one
Postgres, so a conversation created on worker A resolves on worker B, and a save
is visible everywhere. Per-conversation turn serialization uses a
``SELECT … FOR UPDATE`` row lock (see :meth:`turn`).

DDL lives in ``agent_team_studio.agent_studio.postgres`` and is registered from the unified API
lifespan. This module is imported only when Postgres is enabled, so importing it
(which pulls psycopg) never happens on the Postgres-less path.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared.postgres import get_conn
from shared.postgres.metrics import timed_query

from .models import AgentDefinition, ConversationMessage, StudioMode
from .store import ConversationRecord, ConversationTurn

logger = logging.getLogger(__name__)

# ``_STORE`` is the @timed_query metrics label; ``_CONV`` / ``_MSG`` are table
# names. They are all module-level constants (never external input), so
# interpolating ``_CONV`` / ``_MSG`` into SQL is safe — every value is passed as a
# bound ``%s`` parameter. ``_STORE`` coincides with ``_CONV`` today but is a
# separate logical label.
_STORE = "agent_studio_conversations"
_CONV = "agent_studio_conversations"
_MSG = "agent_studio_conv_messages"


class PostgresAgentStudioConversationStore:
    """Postgres-backed store for Agent Studio authoring conversations.

    Stateless with respect to connections — each method acquires its own from the
    pool managed by ``shared.postgres`` and returns it on exit; nothing is held on
    the instance. Invariants mirror the in-memory store's: a ``conversation_id``
    returned by :meth:`create` resolves via :meth:`get` until discarded, and ids
    are never reused.
    """

    @timed_query(store=_STORE, op="create")
    def create(
        self, mode: StudioMode, source_agent_id: str | None, definition: AgentDefinition
    ) -> str:
        """Create a conversation row and return its fresh id.

        Preconditions:
            * ``definition`` is a valid :class:`AgentDefinition` (JSON-serializable).
        Postconditions:
            * Returns a new ``uuid4`` id that :meth:`get` resolves until discarded;
              ids are never reused. The conversation starts with no messages.
        """
        cid = str(uuid4())
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_CONV} "
                "(conversation_id, mode, source_agent_id, definition_json) "
                "VALUES (%s, %s, %s, %s)",
                (cid, mode, source_agent_id, Json(definition.model_dump(mode="json"))),
            )
        return cid

    @timed_query(store=_STORE, op="get")
    def get(self, conversation_id: str) -> ConversationRecord | None:
        """Load a conversation (definition + messages ordered oldest-first) in one query.

        Preconditions:
            * ``conversation_id`` is a string.
        Postconditions:
            * Returns a :class:`ConversationRecord` with messages ordered
              oldest-first, or ``None`` when the id is unknown. The returned record
              is freshly built from the row (no shared mutable state).
        """
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT c.mode, c.source_agent_id, c.definition_json, "
                f"m.role, m.content "
                f"FROM {_CONV} c "
                f"LEFT JOIN {_MSG} m ON m.conversation_id = c.conversation_id "
                "WHERE c.conversation_id = %s ORDER BY m.id",
                (conversation_id,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        head = rows[0]
        messages = [
            ConversationMessage(role=r["role"], content=r["content"])
            for r in rows
            if r["role"] is not None
        ]
        return ConversationRecord(
            conversation_id=conversation_id,
            mode=head["mode"],
            source_agent_id=head["source_agent_id"],
            definition=AgentDefinition.model_validate(head["definition_json"]),
            messages=messages,
        )

    @timed_query(store=_STORE, op="append_message")
    def append_message(self, conversation_id: str, role: str, content: str) -> None:
        """Append one message; bumps the conversation's ``updated_at`` atomically.

        Preconditions:
            * ``conversation_id`` names an existing conversation.
        Postconditions:
            * The message is appended (after all existing messages) and the parent's
              ``updated_at`` is bumped in one statement. Raises :class:`LookupError`
              (→ 404) if the conversation is unknown, matching the in-memory store's
              contract rather than silently inserting an orphan.
        """
        with get_conn() as conn, conn.cursor() as cur:
            # One statement, atomic: the CTE bumps the parent's ``updated_at`` and
            # the INSERT sources its row from that CTE. If the conversation doesn't
            # exist the UPDATE matches nothing → the CTE is empty → the INSERT
            # SELECT gets no source row → ``RETURNING id`` yields nothing, so the
            # ``fetchone() is None`` check below raises rather than orphaning a
            # message.
            cur.execute(
                f"WITH conv AS ("
                f"UPDATE {_CONV} SET updated_at = NOW() "
                "WHERE conversation_id = %s RETURNING conversation_id) "
                f"INSERT INTO {_MSG} (conversation_id, role, content) "
                "SELECT conversation_id, %s, %s FROM conv RETURNING id",
                (conversation_id, role, content),
            )
            if cur.fetchone() is None:
                raise LookupError(f"Unknown conversation: {conversation_id}")

    @timed_query(store=_STORE, op="discard")
    def discard(self, conversation_id: str) -> None:
        """Remove a conversation and its messages (FK cascade).

        Preconditions:
            * ``conversation_id`` is a string.
        Postconditions:
            * ``get(conversation_id)`` returns ``None`` afterward. Idempotent — an
              unknown (or already-discarded) id is a no-op, never an error.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_CONV} WHERE conversation_id = %s", (conversation_id,))

    @timed_query(store=_STORE, op="len")
    def __len__(self) -> int:
        """Return the number of live conversations in the store.

        Preconditions: none.
        Postconditions: returns a non-negative ``COUNT(*)`` of
        ``agent_studio_conversations``.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_CONV}")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    @contextmanager
    def turn(self, conversation_id: str) -> Iterator[ConversationTurn]:
        """Serialize a whole authoring turn with a row lock held across the LLM call.

        Opens one transaction, takes a ``SELECT … FOR UPDATE`` row lock on the
        conversation row, snapshots its definition + messages, and yields a
        :class:`ConversationTurn` whose writes run on the **same** connection. The
        lock is held until the block commits, so a concurrent ``send_message`` on
        the same conversation blocks in its own ``FOR UPDATE`` until this turn
        commits, then reads fresh state — no lost updates, no interleaving. On an
        exception inside the block (e.g. the LLM call fails) the transaction rolls
        back and nothing is persisted (consistent-state-on-failure).

        Tradeoff: a pooled connection + row lock are held for the duration of the
        LLM round trip. Acceptable for the low-concurrency authoring flow (pool max
        defaults to 10 per worker); the frontend already prevents concurrent sends.

        Preconditions:
            * ``conversation_id`` names an existing conversation.
        Postconditions / Exceptions:
            * Yields a :class:`ConversationTurn` snapshotting the current history +
              definition; its ``append_message`` / ``set_definition`` run on the
              locked transaction and commit atomically on clean block exit.
            * Raises :class:`LookupError` (→ 404) if the conversation is unknown.
            * A Postgres error (raised through the ``@timed_query``-wrapped inner
              ops) propagates and rolls the transaction back — nothing is persisted.
        """
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT definition_json FROM {_CONV} WHERE conversation_id = %s FOR UPDATE",
                    (conversation_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"Unknown conversation: {conversation_id}")
                definition = AgentDefinition.model_validate(row["definition_json"])
                cur.execute(
                    f"SELECT role, content FROM {_MSG} WHERE conversation_id = %s ORDER BY id",
                    (conversation_id,),
                )
                history = [(r["role"], r["content"]) for r in cur.fetchall()]

            @timed_query(store=_STORE, op="turn_append_message")
            def _on_message(role: str, content: str) -> None:
                """Append one message on the turn's locked transaction (bumps updated_at).

                Precondition: called only within the enclosing ``turn`` block, while
                its row lock is held. A Postgres error propagates and rolls the whole
                turn back.
                """
                with conn.cursor() as wcur:
                    wcur.execute(
                        f"UPDATE {_CONV} SET updated_at = NOW() WHERE conversation_id = %s",
                        (conversation_id,),
                    )
                    wcur.execute(
                        f"INSERT INTO {_MSG} (conversation_id, role, content) VALUES (%s, %s, %s)",
                        (conversation_id, role, content),
                    )

            @timed_query(store=_STORE, op="turn_set_definition")
            def _on_definition(new_definition: AgentDefinition) -> None:
                """Replace the draft definition on the turn's locked transaction.

                Precondition: called only within the enclosing ``turn`` block. A
                Postgres error propagates and rolls the whole turn back.
                """
                with conn.cursor() as wcur:
                    wcur.execute(
                        f"UPDATE {_CONV} SET definition_json = %s, updated_at = NOW() "
                        "WHERE conversation_id = %s",
                        (Json(new_definition.model_dump(mode="json")), conversation_id),
                    )

            yield ConversationTurn(
                history=history,
                definition=definition,
                on_message=_on_message,
                on_definition=_on_definition,
            )
