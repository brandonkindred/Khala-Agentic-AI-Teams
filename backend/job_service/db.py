"""Postgres persistence layer for the job service.

Stores jobs in a single ``jobs`` table with a JSONB ``data`` column for
team-specific fields.  Top-level columns (status, timestamps) are extracted
for efficient indexing and querying.

DDL is declared in :mod:`job_service.postgres` and applied at startup via
``shared.postgres.register_team_schemas``. This module keeps its own
``psycopg_pool.ConnectionPool`` for high-throughput CRUD (see ``close_pool``
below — it closes this local pool, not the shared one). Same driver
generation (psycopg v3 + psycopg_pool) as ``shared.postgres``, just its own
pool instance sized for CRUD rather than DDL.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool (job-service-local; separate from shared.postgres which
# is only used at startup for DDL).
# ---------------------------------------------------------------------------

_pool: ConnectionPool | None = None


def _dsn() -> str:
    return make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "khala_jobs"),
        user=os.environ.get("POSTGRES_USER", "khala"),
        password=os.environ.get("POSTGRES_PASSWORD", "khala"),
    )


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = ConnectionPool(conninfo=_dsn(), min_size=2, max_size=20, open=True, name="job_service")
    return _pool


@contextmanager
def get_conn() -> Generator:
    pool = get_pool()
    # ``ConnectionPool.connection()`` is itself a context manager that commits
    # on clean exit, rolls back on exception, and returns the connection to
    # the pool — same semantics the old manual getconn/commit/rollback/putconn
    # block implemented by hand for psycopg2.
    with pool.connection() as conn:
        yield conn


def close_pool() -> None:
    global _pool
    if _pool and not _pool.closed:
        _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _row_to_dict(row: tuple, cur) -> dict[str, Any]:
    """Convert a DB row to a merged dict (top-level columns + JSONB data)."""
    col_names = [desc[0] for desc in cur.description]
    row_dict = dict(zip(col_names, row, strict=False))
    data = row_dict.pop("data", {}) or {}
    if isinstance(data, str):
        data = json.loads(data)
    result = {**data}
    result["job_id"] = row_dict["job_id"]
    result["team"] = row_dict["team"]
    result["status"] = row_dict["status"]
    result["created_at"] = row_dict["created_at"].isoformat() if row_dict.get("created_at") else None
    result["updated_at"] = row_dict["updated_at"].isoformat() if row_dict.get("updated_at") else None
    result["last_heartbeat_at"] = (
        row_dict["last_heartbeat_at"].isoformat() if row_dict.get("last_heartbeat_at") else None
    )
    return result


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


def create_job(team: str, job_id: str, status: str = "pending", **fields: Any) -> None:
    now = _now()
    # Extract top-level columns from fields if present, otherwise use defaults
    created_at = fields.pop("created_at", now.isoformat())
    updated_at = fields.pop("updated_at", now.isoformat())
    last_heartbeat_at = fields.pop("last_heartbeat_at", now.isoformat())
    # Remove duplicates from data payload
    fields.pop("job_id", None)
    fields.pop("team", None)
    fields.pop("status", None)
    # Creation is activity: gives every job (including ones that hang while still
    # pending) a baseline for stall detection.
    fields.setdefault("last_activity_at", now.isoformat())

    data_json = json.dumps(fields)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                INSERT INTO jobs (job_id, team, status, data, created_at, updated_at, last_heartbeat_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (team, job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at,
                    last_heartbeat_at = EXCLUDED.last_heartbeat_at
                """,
            (job_id, team, status, data_json, created_at, updated_at, last_heartbeat_at),
        )


def replace_job(team: str, job_id: str, payload: dict[str, Any]) -> None:
    status = payload.get("status", "pending")
    created_at = payload.get("created_at", _now_iso())
    updated_at = payload.get("updated_at", _now_iso())
    last_heartbeat_at = payload.get("last_heartbeat_at", _now_iso())
    # Build data without top-level columns
    data = {
        k: v
        for k, v in payload.items()
        if k not in ("job_id", "team", "status", "created_at", "updated_at", "last_heartbeat_at")
    }
    data_json = json.dumps(data)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                INSERT INTO jobs (job_id, team, status, data, created_at, updated_at, last_heartbeat_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (team, job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    data = EXCLUDED.data,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    last_heartbeat_at = EXCLUDED.last_heartbeat_at
                """,
            (job_id, team, status, data_json, created_at, updated_at, last_heartbeat_at),
        )


def get_job(team: str, job_id: str) -> dict[str, Any] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE team = %s AND job_id = %s", (team, job_id))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row, cur)


def delete_job(team: str, job_id: str) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE team = %s AND job_id = %s", (team, job_id))
        return cur.rowcount > 0


def list_jobs(team: str, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        if statuses:
            cur.execute(
                "SELECT * FROM jobs WHERE team = %s AND status = ANY(%s) ORDER BY created_at DESC",
                (team, statuses),
            )
        else:
            cur.execute(
                "SELECT * FROM jobs WHERE team = %s ORDER BY created_at DESC",
                (team,),
            )
        rows = cur.fetchall()
        return [_row_to_dict(row, cur) for row in rows]


def _prepare_update_fields(fields: dict[str, Any]) -> tuple[str | None, str]:
    """Pop top-level columns out of ``fields`` and stamp ``last_activity_at``.

    Shared preamble for :func:`update_job` and :func:`update_job_if_not_cancelled`
    — both merge arbitrary caller fields into the ``data`` JSONB column and
    promote ``status`` to its own column, so both need the same top-level-column
    scrubbing and activity stamping.

    Preconditions:
        - ``fields`` values are JSON-serializable; this function mutates ``fields``
          in place (pops keys, may add ``last_activity_at``).
    Postconditions:
        - Returns ``(new_status, now)`` where ``new_status`` is the popped
          ``status`` value (or ``None`` if absent) and ``now`` is the server's
          UTC timestamp used for stamping.
        - ``fields`` no longer contains ``status``/``job_id``/``team``/
          ``created_at``/``updated_at``/``last_heartbeat_at`` (those are
          top-level columns, not ``data`` payload).
        - ``fields["last_activity_at"]`` is set to ``now`` unless the caller
          supplied a real (non-``None``) value — every real update counts as
          orchestrator activity, which is what stall detection reads. Pure
          liveness pings must use :func:`heartbeat`, which deliberately does NOT
          touch ``last_activity_at``.
    """
    now = _now_iso()
    new_status = fields.pop("status", None)
    fields.pop("job_id", None)
    fields.pop("team", None)
    fields.pop("created_at", None)
    fields.pop("updated_at", None)
    fields.pop("last_heartbeat_at", None)
    if fields.get("last_activity_at") is None:
        fields["last_activity_at"] = now
    return new_status, now


def _execute_status_update(
    cur: Any,
    team: str,
    job_id: str,
    fields: dict[str, Any],
    new_status: str | None,
    now: str,
    heartbeat: bool,
    guard_cancelled: bool,
) -> bool:
    """Build and run the ``UPDATE jobs SET ...`` statement shared by
    :func:`update_job` and :func:`update_job_if_not_cancelled`.

    Preconditions:
        - ``fields`` has already been prepared by :func:`_prepare_update_fields`
          (no top-level columns left in it).
    Postconditions:
        - Executes exactly one ``UPDATE`` merging ``fields`` into ``data``,
          optionally setting ``status``/``last_heartbeat_at``, and — when
          ``guard_cancelled`` is True — restricting the write to rows where
          ``status != 'cancelled'`` via ``RETURNING job_id``.
        - Returns True when ``guard_cancelled`` is False (a bare UPDATE always
          "succeeds" from the caller's perspective — no row-matched signal is
          available without ``RETURNING``). Returns whether a row was matched
          when ``guard_cancelled`` is True.
    """
    set_clauses = ["data = data || %s::jsonb"]
    params: list[Any] = [json.dumps(fields)]
    if new_status is not None:
        set_clauses.append("status = %s")
        params.append(new_status)
    set_clauses.append("updated_at = %s")
    params.append(now)
    if heartbeat:
        set_clauses.append("last_heartbeat_at = %s")
        params.append(now)
    sql = f"UPDATE jobs SET {', '.join(set_clauses)} WHERE team = %s AND job_id = %s"
    params.extend([team, job_id])
    if guard_cancelled:
        sql += " AND status != 'cancelled' RETURNING job_id"
    cur.execute(sql, tuple(params))
    return cur.fetchone() is not None if guard_cancelled else True


def update_job(team: str, job_id: str, heartbeat: bool = True, **fields: Any) -> None:
    """Merge ``fields`` into the job's data and refresh its timestamps.

    Preconditions:
        - ``fields`` values are JSON-serializable.
    Postconditions:
        - ``data.last_activity_at`` is stamped with the server's UTC now unless the
          caller supplied it explicitly. Every real update — from any team, any
          phase — therefore counts as orchestrator activity, which is what stall
          detection reads. Pure liveness pings must use :func:`heartbeat`, which
          deliberately does NOT touch ``last_activity_at`` (it keeps ticking even
          when the orchestrator thread is hung, so it cannot signal a stall).
    """
    new_status, now = _prepare_update_fields(fields)
    with get_conn() as conn, conn.cursor() as cur:
        _execute_status_update(cur, team, job_id, fields, new_status, now, heartbeat, guard_cancelled=False)


def update_job_if_not_cancelled(team: str, job_id: str, heartbeat: bool = True, **fields: Any) -> bool | None:
    """Merge ``fields`` into the job's data and refresh timestamps, unless cancelled.

    The cancelled-check and the write happen in one conditional ``UPDATE`` (status
    guarded in the ``WHERE`` clause), closing the same check-then-act race
    :func:`cancel_active_job` closes for cancellation itself — but for the opposite
    direction: writing a new RUNNING/COMPLETED/FAILED status without clobbering a
    cancellation that landed first.

    Preconditions:
        - Same as :func:`update_job` — ``fields`` values are JSON-serializable.
        - ``fields`` MAY include ``status``; when present it is written to the
          top-level column (mirrors :func:`update_job`) as long as the job is not
          already cancelled.
        - ``fields["status"]`` must not be ``'cancelled'`` — this primitive only
          guards against overwriting an *existing* cancellation, it does not
          exclude other terminal statuses the way ``cancel_active_job`` does, so
          using it to cancel would silently clobber a completed/failed job.
          Enforced by an explicit raise (a caller bug, not a runtime condition,
          but this guards against silent data corruption so it is not an
          assertion — those are stripped under ``python -O``).
    Postconditions:
        - Returns True and performs the write when the job exists and its status
          is not ``'cancelled'``.
        - Returns False and makes NO write when the job exists but is already
          cancelled — a cancelled job is terminal; the caller's queued status
          transition is silently dropped rather than overwriting the cancellation.
        - Returns None and makes NO write when the job does not exist at all —
          distinct from False so a caller can tell a broken precondition (missing
          row) apart from a legitimate business outcome (cancelled), without a
          second round trip: the disambiguating read below only runs when the
          guarded UPDATE matched zero rows, and stays inside this one call.
        - Guards ONLY on ``'cancelled'`` — unlike :func:`cancel_active_job` this does
          NOT block on other terminal statuses (completed/failed/interrupted),
          matching :func:`is_job_cancelled`'s existing (narrower) check exactly.
    """
    if fields.get("status") == "cancelled":
        raise ValueError(
            "update_job_if_not_cancelled must not be used to cancel a job "
            "(it would overwrite a completed/failed job too) — use cancel_active_job"
        )
    new_status, now = _prepare_update_fields(fields)
    with get_conn() as conn, conn.cursor() as cur:
        updated = _execute_status_update(cur, team, job_id, fields, new_status, now, heartbeat, guard_cancelled=True)
        if updated:
            return True
        # The guarded UPDATE matched zero rows because the job is either missing
        # or already cancelled. One more (cheap, same-connection) query settles
        # which, without a second HTTP round trip for the caller.
        cur.execute("SELECT 1 FROM jobs WHERE team = %s AND job_id = %s", (team, job_id))
        return False if cur.fetchone() is not None else None


def apply_patch(
    team: str,
    job_id: str,
    *,
    merge_fields: dict[str, Any] | None = None,
    merge_nested: dict[str, Any] | None = None,
    append_to: dict[str, list[Any]] | None = None,
    increment: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Atomic read-modify-write: merge fields, merge into nested dicts, append to lists, increment counters.

    Postconditions:
        - ``data.last_activity_at`` is stamped with the server's UTC now unless ``merge_fields``
          supplied it explicitly (a patch is a real update — see :func:`update_job`).
        - Returns the updated job record, read back WITHIN the same row-locked transaction as the
          write — so a caller that increments a counter observes its OWN result, never a value a
          concurrent patch committed afterward (the readback must not be a separate transaction, or
          two racing increments could each read the final value and both mis-conclude they lost).
          Returns None when the job does not exist.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT data, status FROM jobs WHERE team = %s AND job_id = %s FOR UPDATE",
            (team, job_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        current_status = row[1]
        new_status = current_status

        # 1. Merge top-level fields
        if merge_fields:
            if "status" in merge_fields:
                new_status = merge_fields.pop("status")
            data.update(merge_fields)

        # 2. Merge into nested dicts (e.g., "task_states.task_1" -> {"status": "done"})
        if merge_nested:
            for dotted_path, value in merge_nested.items():
                parts = dotted_path.split(".")
                target = data
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                leaf = parts[-1]
                existing = target.get(leaf, {})
                if isinstance(existing, dict) and isinstance(value, dict):
                    existing.update(value)
                    target[leaf] = existing
                else:
                    target[leaf] = value

        # 3. Append to lists
        if append_to:
            for field, items in append_to.items():
                existing_list = data.get(field, [])
                if not isinstance(existing_list, list):
                    existing_list = []
                existing_list.extend(items)
                data[field] = existing_list

        # 4. Increment integer fields
        if increment:
            for field, delta in increment.items():
                current = data.get(field, 0)
                if not isinstance(current, (int, float)):
                    current = 0
                data[field] = current + delta

        now = _now_iso()
        # Same contract as update_job: a real (non-None) value supplied by the
        # caller wins; absent or None gets stamped so the field can never go invalid.
        if not (merge_fields and merge_fields.get("last_activity_at") is not None):
            data["last_activity_at"] = now
        cur.execute(
            """
                UPDATE jobs
                SET data = %s, status = %s, updated_at = %s, last_heartbeat_at = %s
                WHERE team = %s AND job_id = %s
                """,
            (json.dumps(data), new_status, now, now, team, job_id),
        )
        # Read back inside the same row-locked transaction so the returned record reflects THIS
        # patch's write (the FOR UPDATE lock is held until commit, so a concurrent patch can't
        # interleave between this write and read).
        cur.execute("SELECT * FROM jobs WHERE team = %s AND job_id = %s", (team, job_id))
        return _row_to_dict(cur.fetchone(), cur)


def cancel_active_job(team: str, job_id: str) -> bool:
    """Atomically cancel a job only if it is still pending/running.

    Preconditions:
        - ``team`` / ``job_id`` identify a (possibly absent) job.
    Postconditions:
        - The status is set to ``cancelled`` in a single conditional ``UPDATE``
          (status guarded in the ``WHERE`` clause) and ``True`` is returned ONLY
          when the row was pending/running at write time. A job that raced to a
          terminal status (completed/failed/cancelled/interrupted), or does not
          exist, is left untouched and ``False`` is returned. This closes the
          check-then-act window a separate get-then-update would leave open.
    """
    now = _now_iso()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE jobs
                SET status = 'cancelled',
                    updated_at = %s,
                    last_heartbeat_at = %s,
                    data = jsonb_set(data, '{last_activity_at}', %s::jsonb)
                WHERE team = %s AND job_id = %s
                  AND status IN ('pending', 'running')
                RETURNING job_id
                """,
            (now, now, json.dumps(now), team, job_id),
        )
        return cur.fetchone() is not None


def append_event(
    team: str,
    job_id: str,
    *,
    action: str,
    outcome: str | None = None,
    details: dict[str, Any] | None = None,
    status: str | None = None,
) -> None:
    """Append an event to the job's events list and optionally update status.

    Postconditions: ``data.last_activity_at`` is stamped with the server's UTC now —
    an event is a real update (see :func:`update_job`).
    """
    now = _now_iso()
    event = {"timestamp": now, "action": action, "outcome": outcome, "details": details or {}}

    with get_conn() as conn, conn.cursor() as cur:
        if status is not None:
            cur.execute(
                """
                    UPDATE jobs
                    SET data = jsonb_set(
                            jsonb_set(
                                data,
                                '{events}',
                                COALESCE(data->'events', '[]'::jsonb) || %s::jsonb
                            ),
                            '{last_activity_at}',
                            %s::jsonb
                        ),
                        status = %s,
                        updated_at = %s,
                        last_heartbeat_at = %s
                    WHERE team = %s AND job_id = %s
                    """,
                (json.dumps([event]), json.dumps(now), status, now, now, team, job_id),
            )
        else:
            cur.execute(
                """
                    UPDATE jobs
                    SET data = jsonb_set(
                            jsonb_set(
                                data,
                                '{events}',
                                COALESCE(data->'events', '[]'::jsonb) || %s::jsonb
                            ),
                            '{last_activity_at}',
                            %s::jsonb
                        ),
                        updated_at = %s,
                        last_heartbeat_at = %s
                    WHERE team = %s AND job_id = %s
                    """,
                (json.dumps([event]), json.dumps(now), now, now, team, job_id),
            )


def heartbeat(team: str, job_id: str) -> bool:
    """Touch last_heartbeat_at and updated_at for a job. Returns True if the job exists."""
    now = _now_iso()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET last_heartbeat_at = %s, updated_at = %s WHERE team = %s AND job_id = %s",
            (now, now, team, job_id),
        )
        return cur.rowcount > 0


def mark_stale_active_jobs_failed(
    team: str,
    *,
    stale_after_seconds: float,
    reason: str,
    waiting_field: str = "waiting_for_answers",
) -> list[str]:
    """Mark pending/running jobs with no recent heartbeat as failed.

    Excludes jobs in any waiting state (waiting_for_answers, waiting_for_title_selection,
    waiting_for_story_input) — those are paused for user input and should not be
    marked as failed.
    """
    now = _now()
    failed_ids: list[str] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE jobs
                SET status = 'failed',
                    data = data || %s::jsonb,
                    updated_at = %s
                WHERE team = %s
                  AND status IN ('pending', 'running')
                  AND COALESCE((data->>%s)::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_title_selection')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_story_input')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_draft_feedback')::boolean, false) = false
                  AND last_heartbeat_at < %s
                RETURNING job_id
                """,
            (
                json.dumps({"error": reason, "current_activity": None}),
                now.isoformat(),
                team,
                waiting_field,
                (now - __import__("datetime").timedelta(seconds=stale_after_seconds)).isoformat(),
            ),
        )
        failed_ids = [row[0] for row in cur.fetchall()]
    if failed_ids:
        logger.warning("Marked stale jobs failed for team %s: %s", team, failed_ids)
    return failed_ids


def mark_all_active_jobs_failed(team: str, reason: str) -> list[str]:
    """Mark all pending/running jobs as failed.

    Excludes jobs in any waiting state — those are paused for user input.
    """
    now = _now_iso()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE jobs
                SET status = 'failed',
                    data = data || %s::jsonb,
                    updated_at = %s
                WHERE team = %s AND status IN ('pending', 'running')
                  AND COALESCE((data->>'waiting_for_answers')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_title_selection')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_story_input')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_draft_feedback')::boolean, false) = false
                RETURNING job_id
                """,
            (json.dumps({"error": reason, "current_activity": None}), now, team),
        )
        return [row[0] for row in cur.fetchall()]


def mark_all_active_jobs_interrupted(team: str, reason: str) -> list[str]:
    """Mark all pending/running jobs as interrupted (service shutdown).

    Excludes jobs in any waiting state — those are paused for user input.
    """
    now = _now_iso()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE jobs
                SET status = 'interrupted',
                    data = data || %s::jsonb,
                    updated_at = %s
                WHERE team = %s AND status IN ('pending', 'running')
                  AND COALESCE((data->>'waiting_for_answers')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_title_selection')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_story_input')::boolean, false) = false
                  AND COALESCE((data->>'waiting_for_draft_feedback')::boolean, false) = false
                RETURNING job_id
                """,
            (json.dumps({"error": reason, "current_activity": None}), now, team),
        )
        return [row[0] for row in cur.fetchall()]
