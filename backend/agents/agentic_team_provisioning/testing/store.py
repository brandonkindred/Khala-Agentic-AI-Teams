"""Postgres-backed store for interactive testing mode.

Follows the ``startup_advisor/store.py`` pattern exactly:
stateless class, ``@timed_query`` on every method, short-lived
connections via ``shared_postgres.get_conn``.

All DDL lives in ``agentic_team_provisioning.postgres`` and is
registered from the team's FastAPI lifespan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "agentic_team_testing"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _row_ts(value: Any) -> str:
    """Normalize a Postgres TIMESTAMPTZ to an ISO-8601 string."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class AgenticTestStore:
    """Postgres-backed store for test chat sessions, messages, and pipeline runs."""

    def __init__(self) -> None:
        pass  # Stateless; pool lives in shared_postgres

    # ------------------------------------------------------------------
    # Team mode
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="set_team_mode")
    def set_team_mode(self, team_id: str, mode: str) -> None:
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_teams SET mode = %s, updated_at = %s WHERE team_id = %s",
                (mode, now, team_id),
            )

    @timed_query(store=_STORE, op="get_team_mode")
    def get_team_mode(self, team_id: str) -> str:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT mode FROM agentic_teams WHERE team_id = %s", (team_id,))
            row = cur.fetchone()
            return row["mode"] if row else "development"

    # ------------------------------------------------------------------
    # Chat sessions
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_chat_session")
    def create_chat_session(
        self, session_id: str, team_id: str, agent_name: str, session_name: str = ""
    ) -> dict:
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_test_chat_sessions "
                "(session_id, team_id, agent_name, session_name, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (session_id, team_id, agent_name, session_name, now, now),
            )
        return {
            "session_id": session_id,
            "team_id": team_id,
            "agent_name": agent_name,
            "session_name": session_name,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    @timed_query(store=_STORE, op="list_chat_sessions")
    def list_chat_sessions(self, team_id: str, agent_name: Optional[str] = None) -> list[dict]:
        sql = (
            "SELECT session_id, team_id, agent_name, session_name, created_at, updated_at "
            "FROM agentic_test_chat_sessions WHERE team_id = %s"
        )
        params: list[Any] = [team_id]
        if agent_name:
            sql += " AND agent_name = %s"
            params.append(agent_name)
        sql += " ORDER BY updated_at DESC"
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {**r, "created_at": _row_ts(r["created_at"]), "updated_at": _row_ts(r["updated_at"])}
            for r in rows
        ]

    @timed_query(store=_STORE, op="get_chat_session")
    def get_chat_session(self, session_id: str) -> Optional[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT session_id, team_id, agent_name, session_name, created_at, updated_at "
                "FROM agentic_test_chat_sessions WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            **row,
            "created_at": _row_ts(row["created_at"]),
            "updated_at": _row_ts(row["updated_at"]),
        }

    @timed_query(store=_STORE, op="rename_chat_session")
    def rename_chat_session(self, session_id: str, session_name: str) -> bool:
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_test_chat_sessions SET session_name = %s, updated_at = %s "
                "WHERE session_id = %s",
                (session_name, now, session_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="delete_chat_session")
    def delete_chat_session(self, session_id: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            # Messages cascade on delete via FK
            cur.execute(
                "DELETE FROM agentic_test_chat_sessions WHERE session_id = %s",
                (session_id,),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Chat messages
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_chat_message")
    def create_chat_message(
        self, message_id: str, session_id: str, role: str, content: str
    ) -> dict:
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_test_chat_messages "
                "(message_id, session_id, role, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (message_id, session_id, role, content, now),
            )
            # Touch session updated_at
            cur.execute(
                "UPDATE agentic_test_chat_sessions SET updated_at = %s WHERE session_id = %s",
                (now, session_id),
            )
        return {
            "message_id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "rating": None,
            "created_at": now.isoformat(),
        }

    @timed_query(store=_STORE, op="list_chat_messages")
    def list_chat_messages(self, session_id: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT message_id, session_id, role, content, rating, created_at "
                "FROM agentic_test_chat_messages WHERE session_id = %s ORDER BY created_at",
                (session_id,),
            )
            rows = cur.fetchall()
        return [{**r, "created_at": _row_ts(r["created_at"])} for r in rows]

    @timed_query(store=_STORE, op="update_message_rating")
    def update_message_rating(self, message_id: str, rating: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_test_chat_messages SET rating = %s WHERE message_id = %s",
                (rating, message_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="get_agent_quality_scores")
    def get_agent_quality_scores(self, team_id: str) -> list[dict]:
        sql = """
            SELECT s.agent_name,
                   COUNT(m.message_id) FILTER (WHERE m.rating IS NOT NULL) AS total_rated,
                   COUNT(m.message_id) FILTER (WHERE m.rating = 'thumbs_up') AS thumbs_up,
                   COUNT(m.message_id) FILTER (WHERE m.rating = 'thumbs_down') AS thumbs_down
            FROM agentic_test_chat_sessions s
            JOIN agentic_test_chat_messages m ON m.session_id = s.session_id
            WHERE s.team_id = %s AND m.role = 'assistant'
            GROUP BY s.agent_name
        """
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (team_id,))
            rows = cur.fetchall()
        results = []
        for r in rows:
            total = r["total_rated"] or 0
            up = r["thumbs_up"] or 0
            down = r["thumbs_down"] or 0
            pct = (up / total * 100) if total > 0 else 0.0
            results.append(
                {
                    "agent_name": r["agent_name"],
                    "total_rated": total,
                    "thumbs_up": up,
                    "thumbs_down": down,
                    "score_pct": round(pct, 1),
                }
            )
        return results

    # ------------------------------------------------------------------
    # Pipeline runs
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="create_pipeline_run")
    def create_pipeline_run(
        self, run_id: str, team_id: str, process_id: str, initial_input: Optional[str] = None
    ) -> dict:
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_test_pipeline_runs "
                "(run_id, team_id, process_id, status, initial_input, step_results, started_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (run_id, team_id, process_id, "running", initial_input, Json([]), now),
            )
        return {
            "run_id": run_id,
            "team_id": team_id,
            "process_id": process_id,
            "status": "running",
            "current_step_id": None,
            "initial_input": initial_input,
            "step_results": [],
            "human_prompt": None,
            "error": None,
            "started_at": now.isoformat(),
            "finished_at": None,
        }

    @timed_query(store=_STORE, op="get_pipeline_run")
    def get_pipeline_run(self, run_id: str) -> Optional[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT run_id, team_id, process_id, status, current_step_id, "
                "initial_input, step_results, human_prompt, error, started_at, finished_at "
                "FROM agentic_test_pipeline_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            **row,
            "started_at": _row_ts(row["started_at"]),
            "finished_at": _row_ts(row["finished_at"]) if row["finished_at"] else None,
        }

    @timed_query(store=_STORE, op="update_pipeline_run")
    def update_pipeline_run(self, run_id: str, **fields: Any) -> bool:
        if not fields:
            return False
        set_clauses = []
        params: list[Any] = []
        for key, val in fields.items():
            if key == "step_results":
                set_clauses.append("step_results = %s")
                params.append(Json(val))
            else:
                set_clauses.append(f"{key} = %s")
                params.append(val)
        params.append(run_id)
        sql = f"UPDATE agentic_test_pipeline_runs SET {', '.join(set_clauses)} WHERE run_id = %s"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="list_pipeline_runs")
    def list_pipeline_runs(self, team_id: str, limit: int = 20) -> list[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT run_id, team_id, process_id, status, current_step_id, "
                "initial_input, step_results, human_prompt, error, started_at, finished_at "
                "FROM agentic_test_pipeline_runs WHERE team_id = %s "
                "ORDER BY started_at DESC LIMIT %s",
                (team_id, limit),
            )
            rows = cur.fetchall()
        return [
            {
                **r,
                "started_at": _row_ts(r["started_at"]),
                "finished_at": _row_ts(r["finished_at"]) if r["finished_at"] else None,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_default_store: Optional[AgenticTestStore] = None


def get_test_store() -> AgenticTestStore:
    global _default_store  # noqa: PLW0603
    if _default_store is None:
        _default_store = AgenticTestStore()
    return _default_store
