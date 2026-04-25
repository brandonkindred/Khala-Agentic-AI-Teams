"""Postgres-backed store for user agent founder workflow runs and decisions.

Rewritten in PR 3 of the SQLite → Postgres migration. Targets the
``user_agent_founder_runs`` / ``user_agent_founder_decisions`` tables
declared in ``user_agent_founder.postgres`` and registered from the
team's FastAPI lifespan. Public API (constructor, method names,
dataclass shapes) is identical to the pre-migration SQLite version so
``api/main.py`` and ``orchestrator.py`` need no changes.

All data access goes through ``shared_postgres.get_conn`` (pool-backed
since PR 0). Every public method is wrapped in ``@timed_query`` so
slow reads and writes surface as structured log lines.
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from psycopg.rows import dict_row

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "user_agent_founder"

# Columns ``update_run`` is allowed to write. Used both as a whitelist
# (defends against SQL injection via kwargs keys — psycopg3 parameter
# binding is for values only, column names get interpolated via f-string)
# and as a safety net against typo'd call sites.
_UPDATE_ALLOWED_COLUMNS = frozenset(
    {
        "status",
        "se_job_id",
        "analysis_job_id",
        "spec_content",
        "repo_path",
        "target_team_key",
        "error",
    }
)

DEFAULT_TARGET_TEAM_KEY = "software_engineering"


def _row_ts(value: Any) -> str:
    """Normalize a Postgres TIMESTAMPTZ to an ISO-8601 string.

    Preserves the pre-migration dataclass contract where timestamps are
    exposed as strings.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


@dataclass
class StoredRun:
    run_id: str
    status: str
    se_job_id: str | None
    analysis_job_id: str | None
    spec_content: str | None
    repo_path: str | None
    target_team_key: str
    created_at: str
    updated_at: str
    error: str | None


@dataclass
class StoredDecision:
    decision_id: int
    run_id: str
    question_id: str
    question_text: str
    answer_text: str
    rationale: str
    timestamp: str


@dataclass
class StoredChatMessage:
    message_id: int
    run_id: str
    role: str
    content: str
    message_type: str
    metadata: dict[str, Any] | None
    timestamp: str


def _row_to_run(row: dict[str, Any]) -> StoredRun:
    return StoredRun(
        run_id=row["run_id"],
        status=row["status"],
        se_job_id=row["se_job_id"],
        analysis_job_id=row["analysis_job_id"],
        spec_content=row["spec_content"],
        repo_path=row["repo_path"],
        target_team_key=row.get("target_team_key") or DEFAULT_TARGET_TEAM_KEY,
        created_at=_row_ts(row["created_at"]),
        updated_at=_row_ts(row["updated_at"]),
        error=row["error"],
    )


class FounderRunStore:
    """Postgres-backed store for founder agent workflow runs.

    The constructor takes no arguments — the Postgres DSN is read from
    the ``POSTGRES_*`` env vars by ``shared_postgres.get_conn``. The
    lazy ``get_founder_store()`` accessor defers instantiation so
    ``import user_agent_founder.store`` stays cheap.
    """

    def __init__(self) -> None:
        # Stateless; connection pooling lives in shared_postgres.
        pass

    @timed_query(store=_STORE, op="create_run")
    def create_run(self, target_team_key: str = DEFAULT_TARGET_TEAM_KEY) -> str:
        run_id = str(uuid4())
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_agent_founder_runs "
                "(run_id, status, target_team_key, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (run_id, "pending", target_team_key, now, now),
            )
        return run_id

    @timed_query(store=_STORE, op="get_run")
    def get_run(self, run_id: str) -> Optional[StoredRun]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT run_id, status, se_job_id, analysis_job_id, spec_content, "
                "repo_path, target_team_key, created_at, updated_at, error "
                "FROM user_agent_founder_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_run(row)

    @timed_query(store=_STORE, op="update_run")
    def update_run(self, run_id: str, **kwargs: Any) -> bool:
        """Update one or more columns on a run row.

        ``kwargs`` keys are filtered against ``_UPDATE_ALLOWED_COLUMNS``
        before being interpolated into the SET clause — psycopg3
        parameter binding covers values only, so column names MUST come
        from a trusted whitelist to avoid SQL injection.
        """
        if not kwargs:
            return False
        fields = {k: v for k, v in kwargs.items() if k in _UPDATE_ALLOWED_COLUMNS}
        if not fields:
            return False

        # Ordered so set_clause and values stay in lock-step regardless
        # of Python dict iteration order (stable in 3.7+ but explicit is
        # better than implicit for SQL construction).
        ordered_keys = list(fields.keys())
        set_clause = ", ".join(f"{k} = %s" for k in ordered_keys) + ", updated_at = %s"
        values: list[Any] = [fields[k] for k in ordered_keys]
        values.append(datetime.now(tz=timezone.utc))
        values.append(run_id)

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE user_agent_founder_runs SET {set_clause} WHERE run_id = %s",
                values,
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="add_decision")
    def add_decision(
        self,
        run_id: str,
        question_id: str,
        question_text: str,
        answer_text: str,
        rationale: str,
    ) -> int:
        ts = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_agent_founder_decisions "
                "(run_id, question_id, question_text, answer_text, rationale, timestamp) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (run_id, question_id, question_text, answer_text, rationale, ts),
            )
            row = cur.fetchone()
            return int(row[0])

    @timed_query(store=_STORE, op="get_decisions")
    def get_decisions(self, run_id: str) -> list[StoredDecision]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, run_id, question_id, question_text, answer_text, "
                "rationale, timestamp FROM user_agent_founder_decisions "
                "WHERE run_id = %s ORDER BY id",
                (run_id,),
            )
            return [
                StoredDecision(
                    decision_id=int(r["id"]),
                    run_id=r["run_id"],
                    question_id=r["question_id"],
                    question_text=r["question_text"],
                    answer_text=r["answer_text"],
                    rationale=r["rationale"],
                    timestamp=_row_ts(r["timestamp"]),
                )
                for r in cur.fetchall()
            ]

    @timed_query(store=_STORE, op="list_runs")
    def list_runs(self) -> list[StoredRun]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT run_id, status, se_job_id, analysis_job_id, spec_content, "
                "repo_path, target_team_key, created_at, updated_at, error "
                "FROM user_agent_founder_runs ORDER BY created_at DESC"
            )
            return [_row_to_run(r) for r in cur.fetchall()]

    @timed_query(store=_STORE, op="delete_run")
    def delete_run(self, run_id: str) -> bool:
        """Delete a run and its dependent decision + chat rows.

        Returns True if a run row was removed. The schema has no FK
        cascade (see ``user_agent_founder/postgres/__init__.py``), so we
        delete dependents explicitly in the same transaction.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_agent_founder_chat_messages WHERE run_id = %s",
                (run_id,),
            )
            cur.execute(
                "DELETE FROM user_agent_founder_decisions WHERE run_id = %s",
                (run_id,),
            )
            cur.execute(
                "DELETE FROM user_agent_founder_runs WHERE run_id = %s",
                (run_id,),
            )
            return cur.rowcount > 0

    # ── Chat messages ─────────────────────────────────────────────────

    @timed_query(store=_STORE, op="add_chat_message")
    def add_chat_message(
        self,
        run_id: str,
        role: str,
        content: str,
        message_type: str = "chat",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        ts = datetime.now(tz=timezone.utc)
        meta_json = _json.dumps(metadata) if metadata else None
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_agent_founder_chat_messages "
                "(run_id, role, content, message_type, metadata, timestamp) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (run_id, role, content, message_type, meta_json, ts),
            )
            row = cur.fetchone()
            return int(row[0])

    @timed_query(store=_STORE, op="get_chat_messages")
    def get_chat_messages(
        self,
        run_id: str,
        since_id: int = 0,
        limit: int = 200,
    ) -> list[StoredChatMessage]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, run_id, role, content, message_type, metadata, timestamp "
                "FROM user_agent_founder_chat_messages "
                "WHERE run_id = %s AND id > %s ORDER BY id LIMIT %s",
                (run_id, since_id, limit),
            )
            return [
                StoredChatMessage(
                    message_id=int(r["id"]),
                    run_id=r["run_id"],
                    role=r["role"],
                    content=r["content"],
                    message_type=r["message_type"],
                    metadata=r["metadata"],
                    timestamp=_row_ts(r["timestamp"]),
                )
                for r in cur.fetchall()
            ]


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[FounderRunStore] = None


def get_founder_store() -> FounderRunStore:
    """Return the process-wide store, instantiating on first call.

    Lazy so ``import user_agent_founder.store`` never touches Postgres
    — the store itself is stateless; this singleton only exists to
    give tests a stable identity for mocking.
    """
    global _default_store
    if _default_store is None:
        _default_store = FounderRunStore()
    return _default_store
