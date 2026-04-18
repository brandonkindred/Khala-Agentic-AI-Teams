"""Postgres data-access layer for saved inputs and runs.

Follows the branding/blogging pattern:
  * stateless class (pool lives in ``shared_postgres``)
  * one public method per operation, decorated with ``@timed_query``
  * methods translate Postgres errors into typed domain exceptions

When ``POSTGRES_HOST`` is unset, ``shared_postgres.get_conn`` raises, and
we wrap that into :class:`AgentConsoleStorageUnavailable` so the API
layer can return a clean 503.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import uuid4

from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

from .models import RunCreate, RunRecord, RunSummary, SavedInput

logger = logging.getLogger(__name__)
_STORE = "agent_console"


class AgentConsoleStorageUnavailable(RuntimeError):
    """Postgres isn't configured, unreachable, or the pool is shut down."""


class SavedInputNameConflict(ValueError):
    """The ``(agent_id, name)`` unique constraint was violated."""


class AgentConsoleStore:
    """Stateless DAL. Construct once per process; pool is shared."""

    # ------------------------------------------------------------------
    # Saved inputs
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="list_saved_inputs")
    def list_saved_inputs(self, agent_id: str) -> list[SavedInput]:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, agent_id, name, input_data, author, description,
                          created_at, updated_at
                   FROM agent_console_saved_inputs
                   WHERE agent_id = %s
                   ORDER BY created_at DESC""",
                (agent_id,),
            )
            return [SavedInput.model_validate(row) for row in cur.fetchall()]

    @timed_query(store=_STORE, op="get_saved_input")
    def get_saved_input(self, saved_id: str) -> SavedInput | None:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, agent_id, name, input_data, author, description,
                          created_at, updated_at
                   FROM agent_console_saved_inputs
                   WHERE id = %s""",
                (saved_id,),
            )
            row = cur.fetchone()
            return SavedInput.model_validate(row) if row else None

    @timed_query(store=_STORE, op="create_saved_input")
    def create_saved_input(
        self,
        *,
        agent_id: str,
        name: str,
        input_data: Any,
        author: str,
        description: str | None,
    ) -> SavedInput:
        now = _now()
        saved_id = str(uuid4())
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO agent_console_saved_inputs
                          (id, agent_id, name, input_data, author, description,
                           created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (saved_id, agent_id, name, Json(input_data), author, description, now, now),
                )
        except psycopg_errors.UniqueViolation as exc:
            raise SavedInputNameConflict(
                f"Saved input {name!r} already exists for agent {agent_id!r}."
            ) from exc
        return SavedInput(
            id=saved_id,
            agent_id=agent_id,
            name=name,
            input_data=input_data,
            author=author,
            description=description,
            created_at=now,
            updated_at=now,
        )

    @timed_query(store=_STORE, op="update_saved_input")
    def update_saved_input(
        self,
        saved_id: str,
        *,
        name: str | None = None,
        input_data: Any | None = None,
        description: str | None = None,
    ) -> SavedInput | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if input_data is not None:
            sets.append("input_data = %s")
            params.append(Json(input_data))
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        if not sets:
            return self.get_saved_input(saved_id)
        sets.append("updated_at = %s")
        params.append(_now())
        params.append(saved_id)
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    f"UPDATE agent_console_saved_inputs SET {', '.join(sets)} WHERE id = %s",
                    params,
                )
                if cur.rowcount == 0:
                    return None
        except psycopg_errors.UniqueViolation as exc:
            raise SavedInputNameConflict(
                f"Saved input name conflict while updating {saved_id!r}."
            ) from exc
        return self.get_saved_input(saved_id)

    @timed_query(store=_STORE, op="delete_saved_input")
    def delete_saved_input(self, saved_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_console_saved_inputs WHERE id = %s",
                (saved_id,),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    @timed_query(store=_STORE, op="record_run")
    def record_run(self, run: RunCreate) -> RunRecord:
        run_id = str(uuid4())
        created_at = _now()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_console_runs
                      (id, agent_id, team, saved_input_id, input_data, output_data,
                       error, status, duration_ms, trace_id, logs_tail, author,
                       sandbox_url, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    run.agent_id,
                    run.team,
                    run.saved_input_id,
                    Json(run.input_data),
                    Json(run.output_data) if run.output_data is not None else None,
                    run.error,
                    run.status,
                    run.duration_ms,
                    run.trace_id,
                    Json(list(run.logs_tail)),
                    run.author,
                    run.sandbox_url,
                    created_at,
                ),
            )
        return RunRecord(
            id=run_id,
            agent_id=run.agent_id,
            team=run.team,
            saved_input_id=run.saved_input_id,
            status=run.status,
            duration_ms=run.duration_ms,
            trace_id=run.trace_id,
            author=run.author,
            created_at=created_at,
            input_data=run.input_data,
            output_data=run.output_data,
            error=run.error,
            logs_tail=list(run.logs_tail),
            sandbox_url=run.sandbox_url,
        )

    @timed_query(store=_STORE, op="list_runs")
    def list_runs(
        self,
        agent_id: str,
        *,
        limit: int = 50,
        cursor: datetime | None = None,
    ) -> list[RunSummary]:
        limit = max(1, min(limit, 200))
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            if cursor is None:
                cur.execute(
                    """SELECT id, agent_id, team, saved_input_id, status,
                              duration_ms, trace_id, author, created_at
                       FROM agent_console_runs
                       WHERE agent_id = %s
                       ORDER BY created_at DESC
                       LIMIT %s""",
                    (agent_id, limit),
                )
            else:
                cur.execute(
                    """SELECT id, agent_id, team, saved_input_id, status,
                              duration_ms, trace_id, author, created_at
                       FROM agent_console_runs
                       WHERE agent_id = %s AND created_at < %s
                       ORDER BY created_at DESC
                       LIMIT %s""",
                    (agent_id, cursor, limit),
                )
            return [RunSummary.model_validate(row) for row in cur.fetchall()]

    @timed_query(store=_STORE, op="get_run")
    def get_run(self, run_id: str) -> RunRecord | None:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, agent_id, team, saved_input_id, status,
                          duration_ms, trace_id, author, created_at,
                          input_data, output_data, error, logs_tail, sandbox_url
                   FROM agent_console_runs
                   WHERE id = %s""",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return RunRecord.model_validate(row)

    @timed_query(store=_STORE, op="delete_run")
    def delete_run(self, run_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_console_runs WHERE id = %s", (run_id,))
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="prune_runs")
    def prune_runs(self, *, keep_per_agent: int) -> int:
        """Keep only the ``keep_per_agent`` newest rows per ``agent_id``.

        Returns the number of rows deleted. Uses a window function so the
        whole prune is a single round-trip.
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """DELETE FROM agent_console_runs
                   WHERE id IN (
                     SELECT id FROM (
                       SELECT id,
                              row_number() OVER (
                                PARTITION BY agent_id ORDER BY created_at DESC
                              ) AS rn
                       FROM agent_console_runs
                     ) ranked
                     WHERE rn > %s
                   )""",
                (keep_per_agent,),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _conn(self):
        if not is_postgres_enabled():
            raise AgentConsoleStorageUnavailable(
                "POSTGRES_HOST is not configured; Agent Console storage is unavailable."
            )
        try:
            return get_conn()
        except Exception as exc:  # pragma: no cover — infra paths
            raise AgentConsoleStorageUnavailable(str(exc)) from exc


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@lru_cache(maxsize=1)
def get_store() -> AgentConsoleStore:
    return AgentConsoleStore()
