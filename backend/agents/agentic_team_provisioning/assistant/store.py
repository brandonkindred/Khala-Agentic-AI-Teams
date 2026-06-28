"""Postgres-backed persistence for agentic teams and process-design conversations.

Backed by the shared Khala Postgres instance via ``shared_postgres.get_conn``.
DDL lives in ``agentic_team_provisioning.postgres`` and is registered from
the team's FastAPI lifespan.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from agentic_team_provisioning.models import (
    SOURCE_REGISTRY,
    AgenticTeam,
    AgenticTeamAgent,
    ConversationMessage,
    ProcessDefinition,
)
from shared_postgres import get_conn
from shared_postgres.metrics import timed_query
from user_profile import ArtifactType, record_association_safe

logger = logging.getLogger(__name__)

_STORE = "agentic_team_provisioning"


def _row_ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class AgenticTeamStore:
    """Postgres-backed store for teams, processes, and conversations."""

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

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
            created_at=_row_ts(row["created_at"]),
            updated_at=_row_ts(row["updated_at"]),
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
                "created_at": _row_ts(r["created_at"]),
                "updated_at": _row_ts(r["updated_at"]),
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
            cur.execute(
                "INSERT INTO agentic_team_agents "
                "(team_id, agent_name, data_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (team_id, agent_name) DO UPDATE SET "
                "data_json = EXCLUDED.data_json, updated_at = EXCLUDED.updated_at",
                (team_id, a.agent_name, Json(a.model_dump(mode="json")), now, now),
            )
        cur.execute(
            "UPDATE agentic_teams SET updated_at = %s WHERE team_id = %s",
            (now, team_id),
        )

    @timed_query(store=_STORE, op="save_team_agents")
    def save_team_agents(self, team_id: str, agents: list[AgenticTeamAgent]) -> None:
        """Replace the full agents roster for a team (upsert semantics)."""
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            self._write_team_agents(cur, team_id, agents, now)

    @timed_query(store=_STORE, op="merge_generated_agents")
    def merge_generated_agents(
        self, team_id: str, generated: list[AgenticTeamAgent]
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
        from a stale snapshot and drop the other's writes.

        Preconditions: ``team_id`` should name an existing team (callers validate).
        Postconditions: returns the merged roster actually written. If ``team_id`` is
            unknown the roster is left untouched and ``[]`` is returned.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT team_id FROM agentic_teams WHERE team_id = %s FOR UPDATE",
                (team_id,),
            )
            if cur.fetchone() is None:
                return []
            existing = self._load_team_agents(cur, team_id)
            preserved = [a for a in existing if a.source == SOURCE_REGISTRY]
            preserved_names = {a.agent_name for a in preserved}
            merged = preserved + [g for g in generated if g.agent_name not in preserved_names]
            self._write_team_agents(cur, team_id, merged, now)
        return merged

    @timed_query(store=_STORE, op="add_or_replace_team_agent")
    def add_or_replace_team_agent(self, team_id: str, agent: AgenticTeamAgent) -> None:
        """Add (or replace, by name) a single roster agent without disturbing the rest.

        Preconditions: ``team_id`` names an existing team.
        Postconditions: the roster contains exactly one entry named
            ``agent.agent_name`` (the supplied ``agent``); all other entries are
            unchanged. Re-adding an existing name overwrites it in place, preserving
            that row's original ``created_at``.

        Concurrency: a single ``INSERT ... ON CONFLICT`` touches only this agent's
            row (no read-modify-write of the whole roster), so concurrent
            single-agent adds on the same team cannot drop one another's writes.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_team_agents "
                "(team_id, agent_name, data_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (team_id, agent_name) DO UPDATE SET "
                "data_json = EXCLUDED.data_json, updated_at = EXCLUDED.updated_at",
                (team_id, agent.agent_name, Json(agent.model_dump(mode="json")), now, now),
            )
            cur.execute(
                "UPDATE agentic_teams SET updated_at = %s WHERE team_id = %s",
                (now, team_id),
            )

    @timed_query(store=_STORE, op="delete_team_agent")
    def delete_team_agent(self, team_id: str, agent_name: str) -> Optional[AgenticTeamAgent]:
        """Remove a single roster agent by name and return it.

        Postconditions: returns the deleted :class:`AgenticTeamAgent` when an entry
            named ``agent_name`` existed (removing only that row); returns ``None``
            and leaves the roster unchanged otherwise.

        Concurrency: a single ``DELETE ... RETURNING`` removes only this agent's row
            and reports its data atomically — no read-modify-write of the roster, and
            the caller's source check sees the row that was actually deleted.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
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
            return AgenticTeamAgent.model_validate(row["data_json"])

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
                timestamp=_row_ts(r["timestamp"]),
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
                "created_at": _row_ts(r["created_at"]),
                "updated_at": _row_ts(r["updated_at"]),
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
                "created_at": _row_ts(r["created_at"]),
                "updated_at": _row_ts(r["updated_at"]),
            }
            for r in rows
        ]
