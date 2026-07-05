"""Postgres-backed Agent Studio authoring-conversation store.

Durable, cross-worker twin of the in-memory
:class:`~agent_studio.store.AgentStudioConversationStore`. Same public interface
(``create`` / ``get`` / ``append_message`` / ``discard`` / ``__len__`` / ``turn``)
so :class:`~agent_studio.service.AgentStudioService` is agnostic to which one it
holds; the route module selects this one when ``POSTGRES_HOST`` is set.

Multi-worker coherence (the whole point): all four uvicorn workers share one
Postgres, so a conversation created on worker A resolves on worker B, and a save
is visible everywhere. Per-conversation turn serialization uses a
``SELECT … FOR UPDATE`` row lock (see :meth:`turn`).

DDL lives in ``agent_studio.postgres`` and is registered from the unified API
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

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

from .models import AgentDefinition, ConversationMessage, StudioMode
from .store import ConversationRecord, ConversationTurn

logger = logging.getLogger(__name__)

_STORE = "agent_studio_conversations"
_CONV = "agent_studio_conversations"
_MSG = "agent_studio_conv_messages"


class PostgresAgentStudioConversationStore:
    """Postgres-backed store for Agent Studio authoring conversations.

    Stateless; the connection pool lives inside ``shared_postgres``. Invariants
    mirror the in-memory store's: a ``conversation_id`` returned by :meth:`create`
    resolves via :meth:`get` until discarded, and ids are never reused.
    """

    @timed_query(store=_STORE, op="create")
    def create(
        self, mode: StudioMode, source_agent_id: str | None, definition: AgentDefinition
    ) -> str:
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
        """Load a conversation (definition + messages ordered oldest-first) in one query."""
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

        Raises :class:`LookupError` (→ 404) if the conversation is unknown, matching
        the in-memory store's contract (rather than silently inserting an orphan).
        """
        with get_conn() as conn, conn.cursor() as cur:
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
        """Remove a conversation (messages cascade). Idempotent — unknown id is a no-op."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_CONV} WHERE conversation_id = %s", (conversation_id,))

    @timed_query(store=_STORE, op="len")
    def __len__(self) -> int:
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
            * ``conversation_id`` exists (raises :class:`LookupError` → 404 if not).
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

            def _on_message(role: str, content: str) -> None:
                with conn.cursor() as wcur:
                    wcur.execute(
                        f"UPDATE {_CONV} SET updated_at = NOW() WHERE conversation_id = %s",
                        (conversation_id,),
                    )
                    wcur.execute(
                        f"INSERT INTO {_MSG} (conversation_id, role, content) VALUES (%s, %s, %s)",
                        (conversation_id, role, content),
                    )

            def _on_definition(new_definition: AgentDefinition) -> None:
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
