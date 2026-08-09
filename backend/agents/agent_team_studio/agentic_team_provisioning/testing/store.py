"""Postgres-backed store for interactive testing mode.

Follows the ``startup_advisor/store.py`` pattern exactly:
stateless class, ``@timed_query`` on every method, short-lived
connections via ``shared.postgres.get_conn``.

All DDL lives in ``agent_team_studio.agentic_team_provisioning.postgres`` and is
registered from the team's FastAPI lifespan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared.postgres import get_conn
from shared.postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "agentic_team_testing"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _stale_cutoff(now: datetime, stale_seconds: int) -> datetime:
    """Heartbeat-staleness boundary shared by resume and the reaper.

    A single source so the two callers can never diverge: a run is "live" iff its
    ``heartbeat_at >= _stale_cutoff(now, stale_seconds)``, and the reaper fails runs
    strictly older than the same instant — keeping "resumable" and "reap-safe"
    complementary by construction.

    Preconditions: ``stale_seconds`` is a positive int.
    Postconditions: returns ``now - stale_seconds``.
    """
    assert stale_seconds > 0, "stale_seconds must be positive"
    return now - timedelta(seconds=stale_seconds)


def _row_ts(value: Any) -> str:
    """Normalize a Postgres TIMESTAMPTZ to an ISO-8601 string."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


# The DTO-shaped column list for a pipeline run, shared by get/list so the two
# reads can't drift. Deliberately excludes the internal ``human_input`` /
# ``heartbeat_at`` columns, which the ``TestPipelineRun`` response model omits.
_PIPELINE_RUN_COLUMNS = (
    "run_id, team_id, process_id, status, current_step_id, "
    "initial_input, step_results, human_prompt, error, started_at, finished_at"
)


def _normalize_run_row(row: dict) -> dict:
    """Normalize a pipeline-run row's timestamps to ISO strings (shared by get/list)."""
    return {
        **row,
        "started_at": _row_ts(row["started_at"]),
        "finished_at": _row_ts(row["finished_at"]) if row["finished_at"] else None,
    }


class AgenticTestStore:
    """Postgres-backed store for test chat sessions, messages, and pipeline runs."""

    def __init__(self) -> None:
        pass  # Stateless; pool lives in shared.postgres

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
    def update_message_rating(self, team_id: str, message_id: str, rating: str) -> bool:
        """Rate a message, scoped to the team owning its session.

        Postconditions: the rating is applied and ``True`` is returned only when
        ``message_id`` resolves to a message whose session belongs to ``team_id`` —
        a message id from another team's session is a no-op that returns ``False``.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_test_chat_messages m SET rating = %s "
                "FROM agentic_test_chat_sessions s "
                "WHERE m.message_id = %s AND m.session_id = s.session_id AND s.team_id = %s",
                (rating, message_id, team_id),
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
        self,
        run_id: str,
        team_id: str,
        process_id: str,
        initial_input: Optional[str] = None,
        temporal_owned: bool = False,
    ) -> dict:
        """Insert a new pipeline run row.

        Preconditions: ``run_id``/``team_id``/``process_id`` are non-empty strs;
            ``temporal_owned`` is True iff a Temporal workflow (not the in-process
            daemon thread) owns this run's execution and restart recovery.
        Postconditions: a ``running`` row exists with ``heartbeat_at = now`` and
            ``temporal_owned`` persisted; the returned dict mirrors the inserted row.
            When ``temporal_owned`` is True the heartbeat-staleness reaper will skip
            this row (Temporal owns liveness) — see ``reap_orphaned_pipeline_runs``.
        """
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_test_pipeline_runs "
                "(run_id, team_id, process_id, status, initial_input, step_results, "
                "started_at, heartbeat_at, temporal_owned) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                # heartbeat_at is set at creation so the row is never mistaken for a
                # stale orphan in the window before the runner's first heartbeat.
                (
                    run_id,
                    team_id,
                    process_id,
                    "running",
                    initial_input,
                    Json([]),
                    now,
                    now,
                    temporal_owned,
                ),
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

    @timed_query(store=_STORE, op="is_pipeline_run_temporal_owned")
    def is_pipeline_run_temporal_owned(self, run_id: str) -> bool:
        """Return whether a run's execution is owned by a Temporal workflow.

        The submit/cancel endpoints branch on the run's *persisted* owner rather
        than the current process's ``TEMPORAL_ADDRESS`` env, so a config flip
        between dispatch and resume cannot misroute a resume/cancel.

        Preconditions: ``run_id`` is a non-empty str.
        Postconditions: returns the row's ``temporal_owned`` flag, or ``False`` if
            the run does not exist.
        """
        assert run_id, "run_id must be non-empty"
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT temporal_owned FROM agentic_test_pipeline_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        return bool(row["temporal_owned"]) if row else False

    @timed_query(store=_STORE, op="get_pipeline_run")
    def get_pipeline_run(self, run_id: str) -> Optional[dict]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_PIPELINE_RUN_COLUMNS} FROM agentic_test_pipeline_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        return _normalize_run_row(row) if row else None

    @timed_query(store=_STORE, op="get_pipeline_status")
    def get_pipeline_status(self, run_id: str) -> Optional[dict]:
        """Lightweight status + pending answer read for the WAIT poll loop.

        Polling a waiting run every few seconds via ``get_pipeline_run`` would
        re-marshal the (potentially large, ever-growing) ``step_results`` JSON just to
        read one enum; this reads only what the loop needs. Returning ``human_input``
        alongside ``status`` also lets the resume path avoid a second SELECT.

        Preconditions: ``run_id`` is a non-empty str.
        Postconditions: returns ``{"status": str, "human_input": str}`` (``human_input``
        is ``""`` when NULL) or ``None`` if the run does not exist.
        """
        assert run_id, "run_id must be non-empty"
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT status, human_input FROM agentic_test_pipeline_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"status": row["status"], "human_input": row["human_input"] or ""}

    @timed_query(store=_STORE, op="advance_pipeline_step")
    def advance_pipeline_step(self, run_id: str, step_id: str) -> bool:
        """Advance the step cursor iff the run is still ``running`` (one round-trip).

        Merges the per-step terminal check and cursor write: a single conditional
        UPDATE that both moves ``current_step_id`` forward and reports, via rowcount,
        whether the run is still live. Race-tight — no read/write gap in which a
        concurrent cancel/reap could be missed.

        Preconditions: ``run_id`` is a non-empty str; ``step_id`` is a str.
        Postconditions: returns True and sets ``current_step_id`` iff the run was
        ``running``; returns False (no write) if the run reached a terminal state
        out-of-band, signalling the caller to stop.
        """
        assert run_id, "run_id must be non-empty"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_test_pipeline_runs SET current_step_id = %s "
                "WHERE run_id = %s AND status = 'running'",
                (step_id, run_id),
            )
            return cur.rowcount > 0

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
                f"SELECT {_PIPELINE_RUN_COLUMNS} FROM agentic_test_pipeline_runs "
                "WHERE team_id = %s ORDER BY started_at DESC LIMIT %s",
                (team_id, limit),
            )
            rows = cur.fetchall()
        return [_normalize_run_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Pipeline run resume/timeout (compare-and-swap) + liveness
    # ------------------------------------------------------------------
    #
    # The terminal transition out of ``waiting_for_input`` (resume, timeout, or
    # cancel) is a single-row conditional UPDATE. Postgres serializes concurrent
    # UPDATEs to the same row, so exactly one caller observes ``rowcount == 1`` and
    # wins the transition — this is what closes the timeout-vs-submit race without an
    # in-process lock, and what makes resume correct regardless of which uvicorn
    # worker serves the ``/input`` request.

    def _cas_pipeline_run(
        self,
        run_id: str,
        *,
        set_sql: str,
        set_params: tuple,
        where_sql: str,
        where_params: tuple = (),
    ) -> bool:
        """Single-row conditional UPDATE; True iff this caller won the transition.

        The shared core of every ``try_*`` transition: ``UPDATE ... SET {set_sql}
        WHERE run_id = %s AND {where_sql}``. Postgres serializes concurrent updates to
        the row, so exactly one caller sees ``rowcount == 1``.

        Preconditions: ``run_id`` is a non-empty str; the ``%s`` count in ``set_sql`` /
        ``where_sql`` matches ``set_params`` / ``where_params``.
        Postconditions: returns True iff exactly one row matched and was updated.
        """
        assert run_id, "run_id must be non-empty"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE agentic_test_pipeline_runs SET {set_sql} "
                f"WHERE run_id = %s AND {where_sql}",
                (*set_params, run_id, *where_params),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="try_resume_pipeline_run")
    def try_resume_pipeline_run(self, run_id: str, human_input: str, stale_seconds: int) -> bool:
        """Atomically move a *live* waiting run back to ``running`` with the answer.

        The freshness guard (``heartbeat_at`` within ``stale_seconds``) makes resume
        consistent with the reaper: a waiting run is resumable only while a worker is
        actually driving it (its heartbeat thread keeps ``heartbeat_at`` current). An
        orphaned waiting run — e.g. one whose worker died on a restart, whose heartbeat
        has gone stale (or NULL) — is refused here just as the reaper would fail it,
        rather than being resumed into a ``running`` state that no thread can advance.

        Preconditions:
            ``run_id`` is a non-empty str; ``human_input`` is a str (may be empty);
            ``stale_seconds`` is a positive int.
        Postconditions:
            Returns True iff the row was ``waiting_for_input`` with a fresh heartbeat
            and is now ``running`` with ``human_input`` persisted, ``human_prompt``
            cleared, and ``heartbeat_at`` refreshed. Returns False (no-op) if the row
            already left ``waiting_for_input`` (timed out, cancelled, completed, reaped)
            or is an orphan with a stale/absent heartbeat.
        """
        assert stale_seconds > 0, "stale_seconds must be positive"
        now = _now()
        return self._cas_pipeline_run(
            run_id,
            set_sql="status = 'running', human_prompt = NULL, human_input = %s, heartbeat_at = %s",
            set_params=(human_input, now),
            where_sql=(
                "status = 'waiting_for_input' AND heartbeat_at IS NOT NULL AND heartbeat_at >= %s"
            ),
            where_params=(_stale_cutoff(now, stale_seconds),),
        )

    @timed_query(store=_STORE, op="try_resume_pipeline_run_temporal")
    def try_resume_pipeline_run_temporal(self, run_id: str, human_input: str) -> bool:
        """Atomically move a *waiting* Temporal-owned run to ``running`` with the answer.

        Like :meth:`try_resume_pipeline_run` but WITHOUT the heartbeat-freshness guard:
        a Temporal-owned run has no per-run heartbeat thread — the Temporal workflow owns
        its liveness and restart recovery — so ``heartbeat_at`` is not a resumability
        signal for it. Postgres serializes the conditional UPDATE, so exactly one
        concurrent ``/input`` caller wins the transition; this gives the Temporal path
        the same race-free, synchronous resume + 409-on-loss semantics the thread path
        gets from ``try_resume_pipeline_run``, and closes the duplicate-submit window
        where a second signal could overwrite the first answer.

        Preconditions:
            ``run_id`` is a non-empty str; ``human_input`` is a str (may be empty).
        Postconditions:
            Returns True iff the row was ``waiting_for_input`` and is now ``running``
            with ``human_input`` persisted and ``human_prompt`` cleared. Returns False
            (no-op) if the row already left ``waiting_for_input`` (cancelled, timed out,
            completed) — so a resume racing a cancel cannot revive a terminal run.
        """
        return self._cas_pipeline_run(
            run_id,
            set_sql="status = 'running', human_prompt = NULL, human_input = %s",
            set_params=(human_input,),
            where_sql="status = 'waiting_for_input'",
        )

    @timed_query(store=_STORE, op="try_expire_pipeline_run")
    def try_expire_pipeline_run(self, run_id: str, error: str) -> bool:
        """Atomically fail a still-waiting run whose human-input timeout elapsed.

        Preconditions:
            ``run_id`` is a non-empty str; ``error`` is a str.
        Postconditions:
            Returns True iff the row was in ``waiting_for_input`` and is now
            ``failed`` with ``error`` and ``finished_at`` set. Returns False if the
            row already left ``waiting_for_input`` (i.e. a concurrent resume/cancel
            won the race).
        """
        return self._cas_pipeline_run(
            run_id,
            set_sql="status = 'failed', error = %s, finished_at = %s",
            set_params=(error, _now()),
            where_sql="status = 'waiting_for_input'",
        )

    @timed_query(store=_STORE, op="heartbeat_pipeline_run")
    def heartbeat_pipeline_run(self, run_id: str) -> None:
        """Refresh the liveness timestamp for an in-flight run.

        Preconditions: ``run_id`` is a non-empty str.
        Postconditions: sets ``heartbeat_at = now`` iff the run is still
        ``running``/``waiting_for_input``; a no-op once the run is terminal.
        """
        assert run_id, "run_id must be non-empty"
        now = _now()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_test_pipeline_runs SET heartbeat_at = %s "
                "WHERE run_id = %s AND status IN ('running', 'waiting_for_input')",
                (now, run_id),
            )

    @timed_query(store=_STORE, op="try_complete_pipeline_run")
    def try_complete_pipeline_run(self, run_id: str, step_results: list) -> bool:
        """Atomically complete a run, but only if it is still ``running``.

        Guards the terminal write with ``WHERE status = 'running'`` so a run that was
        cancelled, reaped, or expired out-of-band mid-step is not clobbered back to
        ``completed``.

        Preconditions: ``run_id`` is a non-empty str; ``step_results`` is a list.
        Postconditions: returns True iff the row was ``running`` and is now
        ``completed`` with ``step_results`` and ``finished_at`` set; False (no-op) if
        the run already reached a terminal state.
        """
        return self._cas_pipeline_run(
            run_id,
            set_sql="status = 'completed', step_results = %s, finished_at = %s",
            set_params=(Json(step_results), _now()),
            where_sql="status = 'running'",
        )

    @timed_query(store=_STORE, op="try_cancel_pipeline_run")
    def try_cancel_pipeline_run(self, run_id: str) -> bool:
        """Atomically cancel a run, but only if it is still active.

        Guards with ``WHERE status IN ('running', 'waiting_for_input')`` so a cancel
        that races a completed/failed outcome cannot overwrite the real result.

        Preconditions: ``run_id`` is a non-empty str.
        Postconditions: returns True iff the row was active and is now ``cancelled``
        with ``finished_at`` set; False (no-op) if the run was already terminal.
        """
        return self._cas_pipeline_run(
            run_id,
            set_sql="status = 'cancelled', finished_at = %s",
            set_params=(_now(),),
            where_sql="status IN ('running', 'waiting_for_input')",
        )

    @timed_query(store=_STORE, op="try_fail_pipeline_run")
    def try_fail_pipeline_run(self, run_id: str, error: str) -> bool:
        """Atomically fail a run, but only if it is still active.

        Guards with ``WHERE status IN ('running', 'waiting_for_input')`` so an executor
        exception raised after the run was already finalized out-of-band (cancelled by a
        user, or reaped/expired by another actor) cannot clobber that terminal state or
        overwrite its recorded error/finished_at.

        Preconditions: ``run_id`` is a non-empty str; ``error`` is a str.
        Postconditions: returns True iff the row was active and is now ``failed`` with
        ``error`` and ``finished_at`` set; False (no-op) if the run was already terminal.
        """
        return self._cas_pipeline_run(
            run_id,
            set_sql="status = 'failed', error = %s, finished_at = %s",
            set_params=(error, _now()),
            where_sql="status IN ('running', 'waiting_for_input')",
        )

    @timed_query(store=_STORE, op="reap_orphaned_pipeline_runs")
    def reap_orphaned_pipeline_runs(self, error: str, stale_seconds: int) -> int:
        """Fail active runs whose heartbeat has gone stale (orphaned by a dead worker).

        Guarded by a *transaction-scoped* Postgres advisory lock so that, with
        multiple uvicorn workers, only one worker reaps per sweep. The xact-level lock
        (``pg_try_advisory_xact_lock``) is released automatically when the surrounding
        ``get_conn`` transaction commits or rolls back, so a failing UPDATE cannot leak
        the lock onto the pooled connection and permanently disable reaping.

        Staleness is measured on ``heartbeat_at`` (not row age): a live run heartbeats
        within its poll interval, so it is never reaped; an orphaned run stops
        heartbeating and is reaped once it exceeds ``stale_seconds``. Rows with a NULL
        heartbeat (created before this feature) are treated as stale.

        Temporal-owned runs (``temporal_owned = TRUE``) are excluded: they have no
        heartbeat thread (a Temporal workflow drives them and resumes after a
        service restart), so reaping them on heartbeat staleness would wrongly fail a
        run Temporal is about to resume.

        Preconditions:
            ``error`` is a str; ``stale_seconds`` is a positive int.
        Postconditions:
            Returns the number of rows transitioned to ``failed`` (0 if the advisory
            lock was not acquired, i.e. another worker is reaping concurrently). Never
            touches non-active, freshly-heartbeated, or Temporal-owned rows.
        """
        assert stale_seconds > 0, "stale_seconds must be positive"
        now = _now()
        # Stable, arbitrary advisory-lock key for the pipeline-run reaper.
        lock_key = 0x41544D50  # "ATMP"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_key,))
            got = cur.fetchone()
            if not got or not got[0]:
                return 0
            cur.execute(
                "UPDATE agentic_test_pipeline_runs "
                "SET status = 'failed', error = %s, finished_at = %s "
                "WHERE status IN ('running', 'waiting_for_input') "
                "AND NOT temporal_owned "
                "AND (heartbeat_at IS NULL OR heartbeat_at < %s)",
                (error, now, _stale_cutoff(now, stale_seconds)),
            )
            return cur.rowcount


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_default_store: Optional[AgenticTestStore] = None


def get_test_store() -> AgenticTestStore:
    global _default_store  # noqa: PLW0603
    if _default_store is None:
        _default_store = AgenticTestStore()
    return _default_store
