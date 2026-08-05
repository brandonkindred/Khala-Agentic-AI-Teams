"""Postgres-backed store for user agent founder workflow runs and decisions.

Rewritten in PR 3 of the SQLite → Postgres migration. Targets the
``user_agent_founder_runs`` / ``user_agent_founder_decisions`` tables
declared in ``agent_team_studio.user_agent_founder.postgres`` and registered from the
team's FastAPI lifespan. Public API (constructor, method names,
dataclass shapes) is identical to the pre-migration SQLite version so
``api/main.py`` and ``orchestrator.py`` need no changes.

All data access goes through ``shared.postgres.get_conn`` (pool-backed
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

from shared.postgres import get_conn
from shared.postgres.metrics import timed_query

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
        "persona_id",
        "project_name",
        "process_id",
        "error",
    }
)

_PERSONA_UPDATE_ALLOWED = frozenset(
    {
        "name",
        "description",
        "icon",
        "system_prompt",
        "spec_generation_prompt",
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
    persona_id: str | None
    project_name: str | None
    process_id: str | None
    created_at: str
    updated_at: str
    error: str | None


@dataclass
class StoredPersona:
    persona_id: str
    name: str
    description: str
    icon: str
    system_prompt: str
    spec_generation_prompt: str
    is_builtin: bool
    created_at: str
    updated_at: str


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
    """Map a runs row to a :class:`StoredRun`.

    Invariant: an empty-string ``target_team_key`` is propagated verbatim, not
    coerced to the default. ``create_run`` forbids inserting an empty/whitespace
    value, so an empty string read back signals data corruption that should
    surface (e.g. as a failed adapter lookup) rather than be silently masked.
    Only a genuine ``NULL`` falls back to ``DEFAULT_TARGET_TEAM_KEY``.
    """
    # Explicit None check (not ``or``) per the invariant above. Read once into a
    # local to avoid a duplicate dict lookup.
    ttk = row.get("target_team_key")
    return StoredRun(
        run_id=row["run_id"],
        status=row["status"],
        se_job_id=row["se_job_id"],
        analysis_job_id=row["analysis_job_id"],
        spec_content=row["spec_content"],
        repo_path=row["repo_path"],
        target_team_key=ttk if ttk is not None else DEFAULT_TARGET_TEAM_KEY,
        persona_id=row.get("persona_id"),
        project_name=row.get("project_name"),
        process_id=row.get("process_id"),
        created_at=_row_ts(row["created_at"]),
        updated_at=_row_ts(row["updated_at"]),
        error=row["error"],
    )


def _row_to_persona(row: dict[str, Any]) -> StoredPersona:
    return StoredPersona(
        persona_id=row["persona_id"],
        name=row["name"],
        description=row["description"],
        icon=row["icon"],
        system_prompt=row["system_prompt"],
        spec_generation_prompt=row["spec_generation_prompt"],
        is_builtin=bool(row["is_builtin"]),
        created_at=_row_ts(row["created_at"]),
        updated_at=_row_ts(row["updated_at"]),
    )


_RUN_COLUMNS = (
    "run_id, status, se_job_id, analysis_job_id, spec_content, "
    "repo_path, target_team_key, persona_id, project_name, process_id, "
    "created_at, updated_at, error"
)


class FounderRunStore:
    """Postgres-backed store for founder agent workflow runs.

    The constructor takes no arguments — the Postgres DSN is read from
    the ``POSTGRES_*`` env vars by ``shared.postgres.get_conn``. The
    lazy ``get_founder_store()`` accessor defers instantiation so
    ``import agent_team_studio.user_agent_founder.store`` stays cheap.
    """

    def __init__(self) -> None:
        # Stateless; connection pooling lives in shared.postgres.
        pass

    @timed_query(store=_STORE, op="create_run")
    def create_run(
        self,
        target_team_key: str = DEFAULT_TARGET_TEAM_KEY,
        *,
        run_id: str | None = None,
        persona_id: str | None = None,
        project_name: str | None = None,
        process_id: str | None = None,
    ) -> str:
        """Insert a new run row and return its id.

        Preconditions: ``target_team_key`` is non-empty; ``run_id`` (when given)
            is unique. ``process_id`` is the agentic-team process the persona will
            drive (``None`` for the software-engineering target, which has none).
        Postconditions: a row exists with status ``pending`` and the supplied
            ids persisted; the returned id equals ``run_id`` when given, else a
            fresh uuid4 hex.
        """
        # Enforce the contract explicitly (``python -O`` strips ``assert``): an
        # empty (or whitespace-only) target would insert a row with no resolvable
        # adapter.
        if not target_team_key or not target_team_key.strip():
            raise ValueError("create_run: target_team_key must be non-empty")
        # process_id is optional, but an *empty* (or whitespace-only) string is a
        # caller bug (an agentic run needs a real process id; the SE target passes
        # None).
        if process_id is not None and not process_id.strip():
            raise ValueError("create_run: process_id must be non-empty when provided")
        run_id = run_id or str(uuid4())
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_agent_founder_runs "
                "(run_id, status, target_team_key, persona_id, project_name, "
                "process_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    "pending",
                    target_team_key,
                    persona_id,
                    project_name,
                    process_id,
                    now,
                    now,
                ),
            )
        return run_id

    @timed_query(store=_STORE, op="get_run")
    def get_run(self, run_id: str) -> Optional[StoredRun]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_RUN_COLUMNS} FROM user_agent_founder_runs WHERE run_id = %s",
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
                f"SELECT {_RUN_COLUMNS} FROM user_agent_founder_runs ORDER BY created_at DESC"
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


_PERSONA_COLUMNS = (
    "persona_id, name, description, icon, system_prompt, "
    "spec_generation_prompt, is_builtin, created_at, updated_at"
)


class PersonaStore:
    """Postgres-backed CRUD for testing personas.

    Stateless. ``persona_id`` is the slug for builtins ("startup-founder")
    and a uuid hex for user-created rows. ``is_builtin`` is metadata —
    update / delete are NOT guarded against it; the per-spec read-only
    behavior was dropped at user request. Idempotent seeding ensures the
    builtin row reappears on next API restart if it was deleted.
    """

    def __init__(self) -> None:
        pass

    @timed_query(store=_STORE, op="list_personas")
    def list_personas(self) -> list[StoredPersona]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_PERSONA_COLUMNS} FROM user_agent_founder_personas "
                "ORDER BY is_builtin DESC, created_at"
            )
            return [_row_to_persona(r) for r in cur.fetchall()]

    @timed_query(store=_STORE, op="get_persona")
    def get_persona(self, persona_id: str) -> Optional[StoredPersona]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_PERSONA_COLUMNS} FROM user_agent_founder_personas WHERE persona_id = %s",
                (persona_id,),
            )
            row = cur.fetchone()
            return _row_to_persona(row) if row else None

    @timed_query(store=_STORE, op="create_persona")
    def create_persona(
        self,
        *,
        name: str,
        description: str,
        icon: str,
        system_prompt: str,
        spec_generation_prompt: str,
        persona_id: str | None = None,
        is_builtin: bool = False,
    ) -> StoredPersona:
        persona_id = persona_id or uuid4().hex
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO user_agent_founder_personas "
                "(persona_id, name, description, icon, system_prompt, "
                "spec_generation_prompt, is_builtin, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_PERSONA_COLUMNS}",
                (
                    persona_id,
                    name,
                    description,
                    icon,
                    system_prompt,
                    spec_generation_prompt,
                    is_builtin,
                    now,
                    now,
                ),
            )
            return _row_to_persona(cur.fetchone())

    @timed_query(store=_STORE, op="update_persona")
    def update_persona(self, persona_id: str, **kwargs: Any) -> Optional[StoredPersona]:
        fields = {k: v for k, v in kwargs.items() if k in _PERSONA_UPDATE_ALLOWED and v is not None}
        if not fields:
            return self.get_persona(persona_id)
        ordered_keys = list(fields.keys())
        set_clause = ", ".join(f"{k} = %s" for k in ordered_keys) + ", updated_at = %s"
        values: list[Any] = [fields[k] for k in ordered_keys]
        values.append(datetime.now(tz=timezone.utc))
        values.append(persona_id)
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE user_agent_founder_personas SET {set_clause} "
                f"WHERE persona_id = %s RETURNING {_PERSONA_COLUMNS}",
                values,
            )
            row = cur.fetchone()
            return _row_to_persona(row) if row else None

    @timed_query(store=_STORE, op="delete_persona")
    def delete_persona(self, persona_id: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_agent_founder_personas WHERE persona_id = %s",
                (persona_id,),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="seed_builtins")
    def seed_builtins(self) -> bool:
        """Insert the built-in startup-founder persona if missing.

        Idempotent on the slug. If a previous deployment had this row
        and a user deleted it, the next restart re-creates it — this is
        the documented trade-off for keeping seeding simple while leaving
        builtins editable.

        Returns True if a row was inserted, False otherwise.
        """
        # Local import keeps store.py free of agent.py's prompt constants
        # at module load time (avoids potential circular import in tests
        # that import store.py without llm_service available).
        from agent_team_studio.user_agent_founder.agent import (
            FOUNDER_SYSTEM_PROMPT,
            SPEC_GENERATION_PROMPT,
        )

        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_agent_founder_personas "
                "(persona_id, name, description, icon, system_prompt, "
                "spec_generation_prompt, is_builtin, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (persona_id) DO NOTHING",
                (
                    "startup-founder",
                    "Startup Founder",
                    (
                        "Alex Chen — a bootstrapped startup founder building TaskFlow. "
                        "Budget-conscious, speed-first, UX-obsessed. Generates a task management "
                        "product spec and drives the SE team autonomously."
                    ),
                    "rocket_launch",
                    FOUNDER_SYSTEM_PROMPT,
                    SPEC_GENERATION_PROMPT,
                    True,
                    now,
                    now,
                ),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[FounderRunStore] = None
_default_persona_store: Optional[PersonaStore] = None


def get_founder_store() -> FounderRunStore:
    """Return the process-wide store, instantiating on first call.

    Lazy so ``import agent_team_studio.user_agent_founder.store`` never touches Postgres
    — the store itself is stateless; this singleton only exists to
    give tests a stable identity for mocking.
    """
    global _default_store
    if _default_store is None:
        _default_store = FounderRunStore()
    return _default_store


def get_persona_store() -> PersonaStore:
    global _default_persona_store
    if _default_persona_store is None:
        _default_persona_store = PersonaStore()
    return _default_persona_store
