from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from psycopg.types.json import Json

from job_service.db import connection

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _sync_denorm(payload: Dict[str, Any]) -> tuple[str, datetime]:
    status = str(payload.get("status") or "pending")
    hb = _parse_ts(payload.get("last_heartbeat_at")) or _now_utc()
    return status, hb


def get_job(team: str, job_id: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        row = conn.execute(
            "SELECT version, payload FROM jobs WHERE team = %s AND job_id = %s",
            (team, job_id),
        ).fetchone()
    if not row:
        return None
    payload = dict(row["payload"])
    payload["_version"] = row["version"]
    return payload


def put_job(
    team: str,
    job_id: str,
    payload: Dict[str, Any],
    *,
    expected_version: Optional[int],
    unconditional: bool = False,
) -> tuple[bool, int]:
    """Returns (success, new_version). On failure new_version is -1."""
    doc = {k: v for k, v in payload.items() if k != "_version"}
    doc["team"] = team
    doc["job_id"] = job_id
    status, hb = _sync_denorm(doc)
    now = _now_utc()

    with connection() as conn:
        row = conn.execute(
            "SELECT version FROM jobs WHERE team = %s AND job_id = %s",
            (team, job_id),
        ).fetchone()
        if row is None:
            if expected_version is not None and expected_version > 0:
                return False, -1
            v = 1
            conn.execute(
                """INSERT INTO jobs (team, job_id, version, payload, status, last_heartbeat_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (team, job_id, v, Json(doc), status, hb, now),
            )
            return True, v
        current_v = int(row["version"])
        if not unconditional:
            if expected_version is None:
                return False, -1
            if expected_version != current_v:
                return False, -1
        new_v = current_v + 1
        conn.execute(
            """UPDATE jobs SET version = %s, payload = %s, status = %s,
               last_heartbeat_at = %s, updated_at = %s
               WHERE team = %s AND job_id = %s""",
            (new_v, Json(doc), status, hb, now, team, job_id),
        )
        return True, new_v


def delete_job(team: str, job_id: str) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE team = %s AND job_id = %s", (team, job_id))
        return cur.rowcount > 0


def list_jobs(
    *,
    team: Optional[str] = None,
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if team:
        clauses.append("team = %s")
        params.append(team)
    if statuses:
        clauses.append("status = ANY(%s)")
        params.append(statuses)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT payload FROM jobs {where} ORDER BY (payload->>'created_at') DESC NULLS LAST"
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r["payload"]) for r in rows]


def patch_job(
    team: str,
    job_id: str,
    fields: Dict[str, Any],
    *,
    expected_version: Optional[int],
    heartbeat: bool = True,
) -> tuple[bool, int]:
    with connection() as conn:
        row = conn.execute(
            "SELECT version, payload FROM jobs WHERE team = %s AND job_id = %s",
            (team, job_id),
        ).fetchone()
        if not row:
            return False, -1
        current_v = int(row["version"])
        if expected_version is not None and expected_version != current_v:
            return False, -1
        doc = dict(row["payload"])
        skip = {"_version", "team", "job_id"}
        for k, v in fields.items():
            if k in skip:
                continue
            doc[k] = v
        now_s = _now_utc().isoformat()
        doc["updated_at"] = now_s
        if heartbeat:
            doc["last_heartbeat_at"] = now_s
        st, hb = _sync_denorm(doc)
        new_v = current_v + 1
        conn.execute(
            """UPDATE jobs SET version = %s, payload = %s, status = %s,
               last_heartbeat_at = %s, updated_at = %s
               WHERE team = %s AND job_id = %s""",
            (new_v, Json(doc), st, hb, _now_utc(), team, job_id),
        )
        return True, new_v


def heartbeat(
    team: str, job_id: str, *, expected_version: Optional[int] = None
) -> tuple[bool, int]:
    with connection() as conn:
        row = conn.execute(
            "SELECT version, payload FROM jobs WHERE team = %s AND job_id = %s",
            (team, job_id),
        ).fetchone()
        if not row:
            return False, -1
        current_v = int(row["version"])
        if expected_version is not None and expected_version != current_v:
            return False, -1
        doc = dict(row["payload"])
        now_s = _now_utc().isoformat()
        doc["updated_at"] = now_s
        doc["last_heartbeat_at"] = now_s
        st, hb = _sync_denorm(doc)
        new_v = current_v + 1
        conn.execute(
            """UPDATE jobs SET version = %s, payload = %s, status = %s,
               last_heartbeat_at = %s, updated_at = %s
               WHERE team = %s AND job_id = %s""",
            (new_v, Json(doc), st, hb, _now_utc(), team, job_id),
        )
        return True, new_v


def append_event(
    team: str,
    job_id: str,
    *,
    action: str,
    outcome: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> tuple[bool, int]:
    with connection() as conn:
        row = conn.execute(
            "SELECT version, payload FROM jobs WHERE team = %s AND job_id = %s",
            (team, job_id),
        ).fetchone()
        if not row:
            return False, -1
        current_v = int(row["version"])
        if expected_version is not None and expected_version != current_v:
            return False, -1
        doc = dict(row["payload"])
        events = list(doc.get("events") or [])
        now_s = _now_utc().isoformat()
        events.append(
            {
                "timestamp": now_s,
                "action": action,
                "outcome": outcome,
                "details": details or {},
            }
        )
        doc["events"] = events
        if status is not None:
            doc["status"] = status
        doc["updated_at"] = now_s
        doc["last_heartbeat_at"] = now_s
        st, hb = _sync_denorm(doc)
        new_v = current_v + 1
        conn.execute(
            """UPDATE jobs SET version = %s, payload = %s, status = %s,
               last_heartbeat_at = %s, updated_at = %s
               WHERE team = %s AND job_id = %s""",
            (new_v, Json(doc), st, hb, _now_utc(), team, job_id),
        )
        return True, new_v


def mark_stale_failed(
    *,
    stale_after_seconds: float,
    reason: str,
    waiting_field: str = "waiting_for_answers",
) -> List[str]:
    marked: List[str] = []
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT team, job_id, version, payload FROM jobs
            WHERE status IN ('pending', 'running')
            AND (
              payload->>%s IS NULL
              OR LOWER(TRIM(payload->>%s)) NOT IN ('true', '1', 'yes')
            )
            AND last_heartbeat_at < (NOW() AT TIME ZONE 'utc') - %s::interval
            """,
            (waiting_field, waiting_field, f"{float(stale_after_seconds)} seconds"),
        ).fetchall()
        for row in rows:
            team, job_id = row["team"], row["job_id"]
            current_v = int(row["version"])
            doc = dict(row["payload"])
            doc["status"] = "failed"
            doc["error"] = reason
            now_s = _now_utc().isoformat()
            doc["updated_at"] = now_s
            st, hb = _sync_denorm(doc)
            new_v = current_v + 1
            conn.execute(
                """UPDATE jobs SET version = %s, payload = %s, status = %s,
                   last_heartbeat_at = %s, updated_at = %s
                   WHERE team = %s AND job_id = %s""",
                (new_v, Json(doc), st, hb, _now_utc(), team, job_id),
            )
            marked.append(f"{team}/{job_id}")
    if marked:
        logger.warning("Marked stale jobs failed: %s", marked)
    return marked


def fail_active_jobs_for_team(team: str, *, reason: str) -> List[str]:
    """Mark all pending/running jobs for a team as failed (e.g. process shutdown)."""
    marked: List[str] = []
    with connection() as conn:
        rows = conn.execute(
            "SELECT job_id, version, payload FROM jobs WHERE team = %s AND status IN ('pending', 'running')",
            (team,),
        ).fetchall()
        for row in rows:
            job_id = row["job_id"]
            current_v = int(row["version"])
            doc = dict(row["payload"])
            doc["status"] = "failed"
            doc["error"] = reason
            now_s = _now_utc().isoformat()
            doc["updated_at"] = now_s
            st, hb = _sync_denorm(doc)
            new_v = current_v + 1
            conn.execute(
                """UPDATE jobs SET version = %s, payload = %s, status = %s,
                   last_heartbeat_at = %s, updated_at = %s
                   WHERE team = %s AND job_id = %s""",
                (new_v, Json(doc), st, hb, _now_utc(), team, job_id),
            )
            marked.append(job_id)
    return marked


def health_check() -> bool:
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
