"""Postgres-backed persistence for agentic teams and process-design conversations.

Backed by the shared Khala Postgres instance via ``shared.postgres.get_conn``.
DDL lives in ``agentic_team_provisioning.postgres`` and is registered from
the team's FastAPI lifespan.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from agentic_team_provisioning.models import (
    SOURCE_REGISTRY,
    AgenticTeam,
    AgenticTeamAgent,
    ConversationMessage,
    ProcessDefinition,
)
from shared.postgres import get_conn
from shared.postgres.metrics import timed_query
from user_profile import ArtifactType, record_association_safe, remove_association_safe

logger = logging.getLogger(__name__)

_STORE = "agentic_team_provisioning"


class AgenticTeamStore:
    """Postgres-backed store for teams, processes, and conversations."""

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared.postgres.
        pass

    @staticmethod
    def _row_ts(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "")

    # ------------------------------------------------------------------
    # Teams
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_team")
    def create_team(self, name: str, description: str = "") -> AgenticTeam:
        team_id = str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_teams (team_id, name, description, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (team_id, name, description, now, now),
            )
        # Best-effort: link the team to the default profile. record_association_safe
        # never raises, so a link failure can't break team creation.
        record_association_safe(
            ArtifactType.AGENTIC_TEAM, "agentic_team_provisioning", team_id, label=name
        )
        return AgenticTeam(
            team_id=team_id,
            name=name,
            description=description,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )

    @timed_query(store=_STORE, op="delete_team")
    def delete_team(self, team_id: str) -> bool:
        """Delete a team row and its best-effort profile association.

        Compensating action for a ``create_team`` whose subsequent
        infrastructure provisioning failed: the team row already committed,
        but with no infrastructure behind it, so it must not be left listable.
        Safe only in that narrow window — immediately after ``create_team``
        returns, before any process/agent/conversation row exists for the
        team — since this does not cascade-delete dependents.

        Preconditions: ``team_id`` is a non-empty string.
        Postconditions: returns ``True`` iff a row was deleted (and, in that
            case, the best-effort association is also unlinked via
            ``remove_association_safe``). Returns ``False`` (not an error)
            when no such row exists, e.g. a concurrent caller already rolled
            it back.
        """
        assert team_id, "team_id must be non-empty"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agentic_teams WHERE team_id = %s", (team_id,))
            deleted = cur.rowcount > 0
        if deleted:
            # Best-effort: remove_association_safe never raises, so a cleanup
            # failure can't mask the caller's original provisioning error.
            remove_association_safe(ArtifactType.AGENTIC_TEAM, team_id)
        return deleted

    @timed_query(store=_STORE, op="get_team")
    def get_team(self, team_id: str) -> Optional[AgenticTeam]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT team_id, name, description, created_at, updated_at "
                "FROM agentic_teams WHERE team_id = %s",
                (team_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            processes = self._load_processes(cur, team_id)
            agents = self._load_team_agents(cur, team_id)
        return AgenticTeam(
            team_id=row["team_id"],
            name=row["name"],
            description=row["description"],
            agents=agents,
            processes=processes,
            created_at=self._row_ts(row["created_at"]),
            updated_at=self._row_ts(row["updated_at"]),
        )

    @timed_query(store=_STORE, op="list_teams")
    def list_teams(self) -> list[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT t.team_id, t.name, t.description, t.created_at, t.updated_at,
                       (SELECT COUNT(*) FROM agentic_processes p WHERE p.team_id = t.team_id)
                           AS process_count
                FROM agentic_teams t ORDER BY t.created_at DESC
                """
            )
            rows = cur.fetchall()
        return [
            {
                "team_id": r["team_id"],
                "name": r["name"],
                "description": r["description"],
                "process_count": int(r["process_count"] or 0),
                "created_at": self._row_ts(r["created_at"]),
                "updated_at": self._row_ts(r["updated_at"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Processes
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="save_process")
    def save_process(self, team_id: str, process: ProcessDefinition) -> None:
        now = datetime.now(tz=timezone.utc)
        data = process.model_dump(mode="json")
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_processes "
                "(process_id, team_id, data_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (process_id) DO UPDATE SET "
                "data_json = EXCLUDED.data_json, "
                "updated_at = EXCLUDED.updated_at",
                (process.process_id, team_id, Json(data), now, now),
            )
            cur.execute(
                "UPDATE agentic_teams SET updated_at = %s WHERE team_id = %s",
                (now, team_id),
            )

    @timed_query(store=_STORE, op="get_process")
    def get_process(self, process_id: str) -> Optional[ProcessDefinition]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT data_json FROM agentic_processes WHERE process_id = %s",
                (process_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return ProcessDefinition.model_validate(row["data_json"])

    @timed_query(store=_STORE, op="get_process_team_id")
    def get_process_team_id(self, process_id: str) -> Optional[str]:
        """Return the team_id that owns a given process."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT team_id FROM agentic_processes WHERE process_id = %s",
                (process_id,),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    def _load_processes(self, cur, team_id: str) -> list[ProcessDefinition]:
        cur.execute(
            "SELECT data_json FROM agentic_processes WHERE team_id = %s ORDER BY created_at",
            (team_id,),
        )
        return [ProcessDefinition.model_validate(r["data_json"]) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Team agents pool
    # ------------------------------------------------------------------

    def _lock_team(self, cur, team_id: str) -> None:
        """Take the team-row (roster parent) lock on an open cursor, ``FOR UPDATE``.

        Every roster write — full-roster save, merge, single-agent add/delete — calls
        this FIRST, before touching child ``agentic_team_agents`` rows, so all paths
        acquire locks in the same parent→child order and can't deadlock by
        interleaving (a child-first writer racing a parent-first writer would
        otherwise cycle).

        Preconditions: ``cur`` is an open cursor in a live transaction.
        Postconditions: holds a row-level ``FOR UPDATE`` lock on ``team_id``'s
            ``agentic_teams`` row for the rest of the transaction (no-op if the team
            row doesn't exist). Leaves the lock result in the cursor so a caller that
            needs to test existence (e.g. ``merge_generated_agents``) can ``fetchone``.
        """
        cur.execute(
            "SELECT team_id FROM agentic_teams WHERE team_id = %s FOR UPDATE",
            (team_id,),
        )

    def _upsert_team_agent_row(
        self, cur, team_id: str, agent: AgenticTeamAgent, now: datetime
    ) -> None:
        """Upsert one roster row on an open cursor (the single ``INSERT ... ON CONFLICT``
        shared by every roster writer).

        Preconditions: ``cur`` is an open cursor in a live transaction.
        Postconditions: the ``agentic_team_agents`` row for ``(team_id,
            agent.agent_name)`` holds ``agent`` with ``updated_at == now``; an existing
            row keeps its original ``created_at`` (the ``ON CONFLICT`` SET omits it).
            Does NOT touch ``agentic_teams.updated_at`` — callers bump that (once per
            write) so the full-roster save can amortize a single team touch.
        """
        cur.execute(
            "INSERT INTO agentic_team_agents "
            "(team_id, agent_name, data_json, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (team_id, agent_name) DO UPDATE SET "
            "data_json = EXCLUDED.data_json, updated_at = EXCLUDED.updated_at",
            (team_id, agent.agent_name, Json(agent.model_dump(mode="json")), now, now),
        )

    def _write_team_agents(
        self, cur, team_id: str, agents: list[AgenticTeamAgent], now: datetime
    ) -> None:
        """Redefine a team's roster on an open cursor to exactly ``agents``.

        Used by the **full-roster** save (``save_team_agents`` /
        ``merge_generated_agents``), which redefines the whole roster at once. The
        rewrite upserts each surviving row via ``ON CONFLICT`` (so its original
        ``created_at`` is preserved, matching the single-agent helpers) and deletes
        only the rows no longer present, rather than wiping and re-inserting
        everything. A roster that re-includes an existing agent therefore keeps that
        agent's creation time.

        Preconditions: ``cur`` is an open cursor in a live transaction; ``agents``
            have unique ``agent_name`` within the team.
        Postconditions: the team's ``agentic_team_agents`` rows are exactly
            ``agents``; surviving rows retain their ``created_at``; the team's
            ``updated_at`` is bumped to ``now``.
        """
        names = [a.agent_name for a in agents]
        if names:
            # Drop only rows that are no longer on the roster; survivors are upserted
            # below (preserving their created_at) rather than deleted and recreated.
            cur.execute(
                "DELETE FROM agentic_team_agents WHERE team_id = %s AND agent_name <> ALL(%s)",
                (team_id, names),
            )
        else:
            cur.execute("DELETE FROM agentic_team_agents WHERE team_id = %s", (team_id,))
        for a in agents:
            self._upsert_team_agent_row(cur, team_id, a, now)
        cur.execute(
            "UPDATE agentic_teams SET updated_at = %s WHERE team_id = %s",
            (now, team_id),
        )

    @timed_query(store=_STORE, op="save_team_agents")
    def save_team_agents(self, team_id: str, agents: list[AgenticTeamAgent]) -> None:
        """Replace the full agents roster for a team (upsert semantics).

        Preconditions: ``team_id`` is a non-empty string naming an existing team;
            ``agents`` have unique ``agent_name`` within the team.
        Postconditions: the team's ``agentic_team_agents`` rows are exactly ``agents``
            (rows absent from ``agents`` are removed; surviving rows keep their
            ``created_at`` via ``_write_team_agents``'s upsert); the team's
            ``updated_at`` is bumped. If ``team_id`` names no team the agent INSERT
            violates the ``agentic_team_agents`` → ``agentic_teams`` foreign key and
            the transaction raises (only the empty-``agents`` case degrades to a
            no-op DELETE/UPDATE) — callers must pass an existing team id.

        Concurrency: takes the team-row ``FOR UPDATE`` lock before touching child
        ``agentic_team_agents`` rows, so every roster write (this, the single-agent
        helpers, and ``merge_generated_agents``) acquires locks in the same
        parent→child order and they can't deadlock by interleaving.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            self._lock_team(cur, team_id)  # parent-first lock — uniform lock order
            self._write_team_agents(cur, team_id, agents, now)

    @timed_query(store=_STORE, op="merge_generated_agents")
    def merge_generated_agents(
        self,
        team_id: str,
        generated: list[AgenticTeamAgent],
        on_merged: Optional[Callable[[list[AgenticTeamAgent], Any], None]] = None,
    ) -> list[AgenticTeamAgent]:
        """Atomically merge LLM-generated agents into the roster, preserving registry entries.

        The process-design chat round-trips only generated agents, so a naive full
        replace would drop the registry agents a user added via the from-registry
        endpoint (Agent Studio §5.3). This keeps every existing ``source ==
        "registry"`` entry, layers the generated agents on top (a generated agent
        colliding by name with a preserved registry agent is dropped — the
        explicitly-added registry agent wins), and rewrites the roster to the result.

        Concurrency: the read-merge-write runs in a single transaction that first
        takes a ``SELECT ... FOR UPDATE`` row lock on the team, so two concurrent
        merges for the *same* team serialize instead of racing — neither can rewrite
        from a stale snapshot and drop the other's writes. ``on_merged`` (if given) is
        invoked as ``on_merged(merged, conn)`` **under that lock**, before commit, so a
        caller's dependent registry registration can join this same connection/
        transaction (and stay serialized with the single-agent helpers' registry
        cleanup) — closing the gap where a chat-save register could race a concurrent
        add/delete cleanup, and so a later commit failure rolls roster + registry
        writes back together. A raising callback (e.g. ``register_team_manifests`` on
        registry failure) rolls back the roster write so the DB roster and live
        registry stay consistent.

        Preconditions: ``team_id`` should name an existing team (callers validate).
        Postconditions: returns the merged roster actually written (``[]`` if the team
            is unknown — the roster is left untouched in that case). ``on_merged`` (when
            given) is **always** invoked once with the final roster, including the empty
            list for an unknown team, so a caller can still reconcile external state
            (e.g. drop a vanished team's stale registry manifests) — restoring the
            cleanup the pre-callback ``register_team_manifests(team_id, merged)`` did.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock_team(cur, team_id)  # parent-first lock — uniform lock order
            if cur.fetchone() is None:
                # Unknown team: nothing to write, but still let the caller reconcile
                # external state for the (now-absent) team with the empty roster.
                if on_merged is not None:
                    on_merged([], conn)
                return []
            existing = self._load_team_agents(cur, team_id)
            preserved = [a for a in existing if a.source == SOURCE_REGISTRY]
            preserved_names = {a.agent_name for a in preserved}
            merged = preserved + [g for g in generated if g.agent_name not in preserved_names]
            self._write_team_agents(cur, team_id, merged, now)
            if on_merged is not None:
                on_merged(merged, conn)
        return merged

    @timed_query(store=_STORE, op="add_or_replace_team_agent")
    def add_or_replace_team_agent(
        self,
        team_id: str,
        agent: AgenticTeamAgent,
        on_replaced: Optional[Callable[[Optional[AgenticTeamAgent]], None]] = None,
    ) -> None:
        """Add (or replace, by name) a single roster agent without disturbing the rest.

        Preconditions: ``team_id`` names an existing team.
        Postconditions: the roster contains exactly one entry named
            ``agent.agent_name`` (the supplied ``agent``); all other entries are
            unchanged. Re-adding an existing name overwrites it in place, preserving
            that row's original ``created_at``.

        Concurrency: a single ``INSERT ... ON CONFLICT`` touches only this agent's
            row (no read-modify-write of the whole roster), so concurrent
            single-agent adds on the same team cannot drop one another's writes. The
            team-row ``FOR UPDATE`` lock is taken first so this shares the
            parent→child lock order of ``merge_generated_agents`` — a chat-save merge
            and a single-agent write can't deadlock by locking the rows in opposite
            order. ``on_replaced`` (if given) is invoked **under that lock** with the
            row this call replaced (read under the lock, or ``None`` for an insert),
            before commit, so a caller's dependent registry cleanup decides on the
            row actually overwritten and is serialized with the chat-save register —
            closing the read-prior-then-act race. It must be non-raising (best-effort);
            a raising callback would roll back the roster write.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock_team(cur, team_id)  # parent-first lock — uniform lock order
            # Read the row we're about to replace under the lock, so a caller's
            # cleanup acts on the truly-replaced row (not a pre-lock snapshot).
            prior = self._get_team_agent(cur, team_id, agent.agent_name)
            self._upsert_team_agent_row(cur, team_id, agent, now)
            cur.execute(
                "UPDATE agentic_teams SET updated_at = %s WHERE team_id = %s",
                (now, team_id),
            )
            if on_replaced is not None:
                on_replaced(prior)

    @timed_query(store=_STORE, op="update_team_agent")
    def update_team_agent(
        self,
        team_id: str,
        agent_name: str,
        apply_updates: Callable[[AgenticTeamAgent], AgenticTeamAgent],
        on_updated: Optional[Callable[[AgenticTeamAgent], None]] = None,
    ) -> Optional[AgenticTeamAgent]:
        """Atomically read-modify-write a single roster agent under the team lock.

        ``apply_updates`` receives the agent row read **under the lock** and returns
        the row to persist (the caller merges its patch onto that fresh row and
        re-validates). Because the read, the merge, and the write all happen inside
        one locked transaction, a concurrent roster write for the same agent (e.g. a
        chat-save filling ``skills`` while the user saves a ``role`` edit) cannot be
        clobbered by a merge over a pre-lock snapshot.

        Preconditions: ``team_id`` and ``agent_name`` are non-empty strings;
            ``apply_updates`` must not change ``agent_name`` (the row is written under
            the same key it was read by).
        Postconditions: returns the persisted (updated) agent when a row named
            ``agent_name`` existed; returns ``None`` and leaves the roster unchanged
            when the team or the agent is unknown. If ``apply_updates`` raises (e.g. a
            validation error), the transaction rolls back and the exception propagates
            (the roster is unchanged). ``on_updated`` (if given) runs **under the lock**
            with the persisted row before commit, so a caller's dependent registry
            reconciliation is serialized with the chat-save register — it must be
            non-raising (a raising callback would roll back the write).
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock_team(cur, team_id)  # parent-first lock — uniform lock order
            current = self._get_team_agent(cur, team_id, agent_name)
            if current is None:
                return None
            updated = apply_updates(current)
            # ``apply_updates`` must not change ``agent_name`` (see docstring), so the
            # row is written under the same key it was read by.
            self._upsert_team_agent_row(cur, team_id, updated, now)
            cur.execute(
                "UPDATE agentic_teams SET updated_at = %s WHERE team_id = %s",
                (now, team_id),
            )
            if on_updated is not None:
                on_updated(updated)
            return updated

    @timed_query(store=_STORE, op="delete_team_agent")
    def delete_team_agent(
        self,
        team_id: str,
        agent_name: str,
        on_deleted: Optional[Callable[[AgenticTeamAgent], None]] = None,
    ) -> Optional[AgenticTeamAgent]:
        """Remove a single roster agent by name and return it.

        Preconditions: ``team_id`` and ``agent_name`` are non-empty strings.
        Postconditions: returns the deleted :class:`AgenticTeamAgent` when an entry
            named ``agent_name`` existed (removing only that row); returns ``None``
            and leaves the roster unchanged otherwise.

        Concurrency: a single ``DELETE ... RETURNING`` removes only this agent's row
            and reports its data atomically — no read-modify-write of the roster, and
            the caller's source check sees the row that was actually deleted. The
            team-row ``FOR UPDATE`` lock is taken first so this shares the
            parent→child lock order of ``merge_generated_agents`` — a chat-save merge
            and a concurrent delete can't deadlock by locking the parent and child
            rows in opposite order. ``on_deleted`` (if given) is invoked **under that
            lock** with the deleted row, before commit, so a caller's dependent
            registry cleanup is serialized with the chat-save register — a concurrent
            re-add+register can't slip between the delete and the cleanup. It must be
            non-raising (best-effort); a raising callback would roll back the delete.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Parent-first lock (see _lock_team) — uniform lock order. The lock
            # SELECT leaves its own result set on the cursor, but the DELETE ...
            # RETURNING below re-executes the cursor and replaces it, so the
            # fetchone() that follows reads the deleted row, not the lock SELECT.
            self._lock_team(cur, team_id)
            cur.execute(
                "DELETE FROM agentic_team_agents WHERE team_id = %s AND agent_name = %s "
                "RETURNING data_json",
                (team_id, agent_name),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE agentic_teams SET updated_at = %s WHERE team_id = %s",
                (now, team_id),
            )
            deleted = AgenticTeamAgent.model_validate(row["data_json"])
            if on_deleted is not None:
                on_deleted(deleted)
            return deleted

    @timed_query(store=_STORE, op="list_team_agents")
    def list_team_agents(self, team_id: str) -> list[AgenticTeamAgent]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            return self._load_team_agents(cur, team_id)

    def _load_team_agents(self, cur, team_id: str) -> list[AgenticTeamAgent]:
        cur.execute(
            "SELECT data_json FROM agentic_team_agents WHERE team_id = %s ORDER BY agent_name",
            (team_id,),
        )
        return [AgenticTeamAgent.model_validate(r["data_json"]) for r in cur.fetchall()]

    def _get_team_agent(self, cur, team_id: str, agent_name: str) -> Optional[AgenticTeamAgent]:
        """Read one roster agent by name on an open cursor (under the caller's lock).

        Preconditions: ``cur`` is an open cursor in a live transaction.
        Postconditions: returns the :class:`AgenticTeamAgent` named ``agent_name`` for
            ``team_id`` if present, else ``None``. Reads only — no mutation.
        """
        cur.execute(
            "SELECT data_json FROM agentic_team_agents WHERE team_id = %s AND agent_name = %s",
            (team_id, agent_name),
        )
        row = cur.fetchone()
        return AgenticTeamAgent.model_validate(row["data_json"]) if row else None

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_conversation")
    def create_conversation(self, team_id: str) -> str:
        conversation_id = str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_conversations "
                "(conversation_id, team_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (conversation_id, team_id, now, now),
            )
        return conversation_id

    @timed_query(store=_STORE, op="get_conversation_team_id")
    def get_conversation_team_id(self, conversation_id: str) -> Optional[str]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT team_id FROM agentic_conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    @timed_query(store=_STORE, op="get_conversation_process_id")
    def get_conversation_process_id(self, conversation_id: str) -> Optional[str]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT process_id FROM agentic_conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return str(row[0]) if row[0] is not None else None

    @timed_query(store=_STORE, op="set_conversation_process")
    def set_conversation_process(self, conversation_id: str, process_id: str) -> None:
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_conversations SET process_id = %s, updated_at = %s "
                "WHERE conversation_id = %s",
                (process_id, now, conversation_id),
            )

    @timed_query(store=_STORE, op="append_message")
    def append_message(self, conversation_id: str, role: str, content: str) -> None:
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_conv_messages "
                "(conversation_id, role, content, timestamp) VALUES (%s, %s, %s, %s)",
                (conversation_id, role, content, now),
            )
            cur.execute(
                "UPDATE agentic_conversations SET updated_at = %s WHERE conversation_id = %s",
                (now, conversation_id),
            )

    @timed_query(store=_STORE, op="get_messages")
    def get_messages(self, conversation_id: str) -> list[ConversationMessage]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT role, content, timestamp FROM agentic_conv_messages "
                "WHERE conversation_id = %s ORDER BY id",
                (conversation_id,),
            )
            rows = cur.fetchall()
        return [
            ConversationMessage(
                role=r["role"],
                content=r["content"],
                timestamp=self._row_ts(r["timestamp"]),
            )
            for r in rows
        ]

    @timed_query(store=_STORE, op="list_conversations")
    def list_conversations(self, team_id: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT c.conversation_id, c.team_id, c.created_at, c.updated_at,
                       (SELECT COUNT(*) FROM agentic_conv_messages m
                            WHERE m.conversation_id = c.conversation_id) AS message_count
                FROM agentic_conversations c
                WHERE c.team_id = %s
                ORDER BY c.created_at DESC
                """,
                (team_id,),
            )
            rows = cur.fetchall()
        return [
            {
                "conversation_id": str(r["conversation_id"]),
                "team_id": str(r["team_id"]),
                "created_at": self._row_ts(r["created_at"]),
                "updated_at": self._row_ts(r["updated_at"]),
                "message_count": int(r["message_count"] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Agent Provisioning bridge (per-step agent environments)
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="try_begin_agent_env_provision")
    def try_begin_agent_env_provision(
        self,
        team_id: str,
        stable_key: str,
        process_id: str,
        step_id: str,
        agent_name: str,
        provisioning_agent_id: str,
    ) -> bool:
        """Return True if a new provisioning run should start (caller spawns thread).

        Uses ``INSERT ... ON CONFLICT`` with a conditional UPDATE so the
        decision is atomic at the database level. The CTE pattern returns
        the previous status (if any) and the current one, so we can decide
        whether this caller is the one that transitioned the row to
        ``running`` and should therefore own the background thread.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH prev AS (
                    SELECT status FROM agentic_env_provisions
                    WHERE team_id = %s AND stable_key = %s
                ),
                up AS (
                    INSERT INTO agentic_env_provisions (
                        team_id, stable_key, process_id, step_id, agent_name,
                        provisioning_agent_id, status, error_message,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'running', NULL, %s, %s)
                    ON CONFLICT (team_id, stable_key) DO UPDATE SET
                        provisioning_agent_id = EXCLUDED.provisioning_agent_id,
                        process_id = EXCLUDED.process_id,
                        step_id = EXCLUDED.step_id,
                        agent_name = EXCLUDED.agent_name,
                        status = 'running',
                        error_message = NULL,
                        updated_at = EXCLUDED.updated_at
                    WHERE agentic_env_provisions.status = 'failed'
                    RETURNING status
                )
                SELECT (SELECT status FROM prev) AS prev_status,
                       (SELECT status FROM up)   AS new_status
                """,
                (
                    team_id,
                    stable_key,
                    team_id,
                    stable_key,
                    process_id,
                    step_id,
                    agent_name,
                    provisioning_agent_id,
                    now,
                    now,
                ),
            )
            row = cur.fetchone() or {}
            prev = row.get("prev_status")
            new = row.get("new_status")

        # New row inserted (previous row didn't exist, INSERT succeeded → status is 'running').
        if prev is None and new == "running":
            return True
        # Row existed and was 'failed'; the UPDATE fired and moved it to 'running'.
        if prev == "failed" and new == "running":
            return True
        # Already 'running' or 'completed' — no-op.
        return False

    @timed_query(store=_STORE, op="mark_agent_env_provision_finished")
    def mark_agent_env_provision_finished(
        self,
        team_id: str,
        stable_key: str,
        *,
        success: bool,
        error_message: str | None,
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        status = "completed" if success else "failed"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_env_provisions SET "
                "status = %s, error_message = %s, updated_at = %s "
                "WHERE team_id = %s AND stable_key = %s",
                (status, error_message, now, team_id, stable_key),
            )

    @timed_query(store=_STORE, op="list_agent_env_provisions")
    def list_agent_env_provisions(self, team_id: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT stable_key, process_id, step_id, agent_name, provisioning_agent_id,
                       status, error_message, created_at, updated_at
                FROM agentic_env_provisions
                WHERE team_id = %s
                ORDER BY updated_at DESC
                """,
                (team_id,),
            )
            rows = cur.fetchall()
        return [
            {
                "stable_key": r["stable_key"],
                "process_id": r["process_id"],
                "step_id": r["step_id"],
                "agent_name": r["agent_name"],
                "provisioning_agent_id": r["provisioning_agent_id"],
                "status": r["status"],
                "error_message": r["error_message"],
                "created_at": self._row_ts(r["created_at"]),
                "updated_at": self._row_ts(r["updated_at"]),
            }
            for r in rows
        ]
