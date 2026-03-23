"""Postgres persistence layer for the job service.

Stores jobs in a single ``jobs`` table with a JSONB ``data`` column for
team-specific fields.  Top-level columns (status, timestamps) are extracted
for efficient indexing and querying.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _dsn() -> str:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "strands")
    password = os.environ.get("POSTGRES_PASSWORD", "strands")
    dbname = os.environ.get("POSTGRES_DB", "strands_jobs")
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=20, dsn=_dsn())
    return _pool


@contextmanager
def get_conn() -> Generator:
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def ensure_schema() -> None:
    """Create the jobs table and indexes if they do not already exist."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id            TEXT NOT NULL,
                    team              TEXT NOT NULL,
                    status            TEXT NOT NULL DEFAULT 'pending',
                    data              JSONB NOT NULL DEFAULT '{}',
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (team, job_id)
                )
            """)
        cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_team_status
                ON jobs (team, status)
            """)
        cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_heartbeat
                ON jobs (team, last_heartbeat_at)
                WHERE status IN ('pending', 'running')
            """)
    logger.info("Job service schema ensured")


def close_pool() -> None:
    global _pool
    if _pool and not _pool.closed:
        _pool.closeall()
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


def update_job(team: str, job_id: str, heartbeat: bool = True, **fields: Any) -> None:
    now = _now_iso()
    # If status is being updated, update the top-level column too
    new_status = fields.pop("status", None)
    # Remove other top-level columns from fields if present
    fields.pop("job_id", None)
    fields.pop("team", None)
    fields.pop("created_at", None)
    fields.pop("updated_at", None)
    fields.pop("last_heartbeat_at", None)

    with get_conn() as conn, conn.cursor() as cur:
        if new_status is not None:
            if heartbeat:
                cur.execute(
                    """
                        UPDATE jobs
                        SET data = data || %s::jsonb,
                            status = %s,
                            updated_at = %s,
                            last_heartbeat_at = %s
                        WHERE team = %s AND job_id = %s
                        """,
                    (json.dumps(fields), new_status, now, now, team, job_id),
                )
            else:
                cur.execute(
                    """
                        UPDATE jobs
                        SET data = data || %s::jsonb,
                            status = %s,
                            updated_at = %s
                        WHERE team = %s AND job_id = %s
                        """,
                    (json.dumps(fields), new_status, now, team, job_id),
                )
        else:
            if heartbeat:
                cur.execute(
                    """
                        UPDATE jobs
                        SET data = data || %s::jsonb,
                            updated_at = %s,
                            last_heartbeat_at = %s
                        WHERE team = %s AND job_id = %s
                        """,
                    (json.dumps(fields), now, now, team, job_id),
                )
            else:
                cur.execute(
                    """
                        UPDATE jobs
                        SET data = data || %s::jsonb,
                            updated_at = %s
                        WHERE team = %s AND job_id = %s
                        """,
                    (json.dumps(fields), now, team, job_id),
                )


def apply_patch(
    team: str,
    job_id: str,
    *,
    merge_fields: dict[str, Any] | None = None,
    merge_nested: dict[str, Any] | None = None,
    append_to: dict[str, list[Any]] | None = None,
    increment: dict[str, int] | None = None,
) -> None:
    """Atomic read-modify-write: merge fields, merge into nested dicts, append to lists, increment counters."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT data, status FROM jobs WHERE team = %s AND job_id = %s FOR UPDATE",
            (team, job_id),
        )
        row = cur.fetchone()
        if row is None:
            return
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
        cur.execute(
            """
                UPDATE jobs
                SET data = %s, status = %s, updated_at = %s, last_heartbeat_at = %s
                WHERE team = %s AND job_id = %s
                """,
            (json.dumps(data), new_status, now, now, team, job_id),
        )


def append_event(
    team: str,
    job_id: str,
    *,
    action: str,
    outcome: str | None = None,
    details: dict[str, Any] | None = None,
    status: str | None = None,
) -> None:
    """Append an event to the job's events list and optionally update status."""
    now = _now_iso()
    event = {"timestamp": now, "action": action, "outcome": outcome, "details": details or {}}

    with get_conn() as conn, conn.cursor() as cur:
        if status is not None:
            cur.execute(
                """
                    UPDATE jobs
                    SET data = jsonb_set(
                            data,
                            '{events}',
                            COALESCE(data->'events', '[]'::jsonb) || %s::jsonb
                        ),
                        status = %s,
                        updated_at = %s,
                        last_heartbeat_at = %s
                    WHERE team = %s AND job_id = %s
                    """,
                (json.dumps([event]), status, now, now, team, job_id),
            )
        else:
            cur.execute(
                """
                    UPDATE jobs
                    SET data = jsonb_set(
                            data,
                            '{events}',
                            COALESCE(data->'events', '[]'::jsonb) || %s::jsonb
                        ),
                        updated_at = %s,
                        last_heartbeat_at = %s
                    WHERE team = %s AND job_id = %s
                    """,
                (json.dumps([event]), now, now, team, job_id),
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

    Excludes jobs where data->>waiting_field is 'true'.
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
                  AND last_heartbeat_at < %s
                RETURNING job_id
                """,
            (
                json.dumps({"error": reason}),
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
    """Mark all pending/running jobs as failed (e.g. on shutdown)."""
    now = _now_iso()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
                UPDATE jobs
                SET status = 'failed',
                    data = data || %s::jsonb,
                    updated_at = %s
                WHERE team = %s AND status IN ('pending', 'running')
                RETURNING job_id
                """,
            (json.dumps({"error": reason}), now, team),
        )
        return [row[0] for row in cur.fetchall()]
