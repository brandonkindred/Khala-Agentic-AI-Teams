"""Per-team infrastructure scaffolding: directories, SQLite database, and job client.

When a team is created via the provisioning API, ``provision_team`` creates:
- ``$AGENT_CACHE/provisioned_teams/{team_id}/assets/``  — file artifacts
- ``$AGENT_CACHE/provisioned_teams/{team_id}/runs/``    — job working directories
- ``$AGENT_CACHE/provisioned_teams/{team_id}/team.db``  — SQLite with form_data table

All operations are idempotent.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from job_service_client import JobServiceClient

logger = logging.getLogger(__name__)

_AGENT_CACHE = os.getenv("AGENT_CACHE", os.path.join(os.path.expanduser("~"), ".agent_cache"))

_FORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS form_data (
    record_id  TEXT PRIMARY KEY,
    form_key   TEXT NOT NULL,
    data_json  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_form_data_key ON form_data(form_key);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# TeamFormStore — thin SQLite wrapper for per-team form data
# ---------------------------------------------------------------------------


class TeamFormStore:
    """Thread-safe SQLite store for structured form records."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_FORM_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def create_record(self, form_key: str, data: Dict[str, Any]) -> Dict[str, Any]:
        record_id = str(uuid4())
        now = _now_iso()
        data_json = json.dumps(data)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO form_data (record_id, form_key, data_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (record_id, form_key, data_json, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return {
            "record_id": record_id,
            "form_key": form_key,
            "data": data,
            "created_at": now,
            "updated_at": now,
        }

    def get_records(self, form_key: str) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT record_id, form_key, data_json, created_at, updated_at"
                    " FROM form_data WHERE form_key = ? ORDER BY created_at",
                    (form_key,),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "record_id": r["record_id"],
                "form_key": r["form_key"],
                "data": json.loads(r["data_json"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def get_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT record_id, form_key, data_json, created_at, updated_at"
                    " FROM form_data WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return {
            "record_id": row["record_id"],
            "form_key": row["form_key"],
            "data": json.loads(row["data_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_record(self, record_id: str, data: Dict[str, Any]) -> bool:
        now = _now_iso()
        data_json = json.dumps(data)
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute(
                    "UPDATE form_data SET data_json = ?, updated_at = ? WHERE record_id = ?",
                    (data_json, now, record_id),
                )
                conn.commit()
            finally:
                conn.close()
        return result.rowcount > 0

    def delete_record(self, record_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute("DELETE FROM form_data WHERE record_id = ?", (record_id,))
                conn.commit()
            finally:
                conn.close()
        return result.rowcount > 0

    def list_form_keys(self) -> List[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT form_key FROM form_data ORDER BY form_key"
                ).fetchall()
            finally:
                conn.close()
        return [r["form_key"] for r in rows]


# ---------------------------------------------------------------------------
# TeamInfrastructure — per-team resource handles
# ---------------------------------------------------------------------------


@dataclass
class TeamInfrastructure:
    """Holds paths and clients for a provisioned team's infrastructure."""

    team_id: str
    base_dir: Path
    assets_dir: Path
    runs_dir: Path
    db_path: Path
    job_client: JobServiceClient
    form_store: TeamFormStore = field(repr=False)


# ---------------------------------------------------------------------------
# Provisioning functions
# ---------------------------------------------------------------------------

_infra_cache: Dict[str, TeamInfrastructure] = {}
_infra_lock = threading.Lock()


def provision_team(team_id: str) -> TeamInfrastructure:
    """Create per-team directories, database, and job client. Idempotent."""
    base = Path(_AGENT_CACHE) / "provisioned_teams" / team_id
    assets_dir = base / "assets"
    runs_dir = base / "runs"
    db_path = base / "team.db"

    assets_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    form_store = TeamFormStore(str(db_path))
    job_client = JobServiceClient(team=f"provisioned_{team_id}")

    infra = TeamInfrastructure(
        team_id=team_id,
        base_dir=base,
        assets_dir=assets_dir,
        runs_dir=runs_dir,
        db_path=db_path,
        job_client=job_client,
        form_store=form_store,
    )

    with _infra_lock:
        _infra_cache[team_id] = infra

    logger.info("Provisioned infrastructure for team %s at %s", team_id, base)
    return infra


def get_team_infrastructure(team_id: str) -> TeamInfrastructure:
    """Return cached infrastructure for a team, provisioning lazily if needed."""
    with _infra_lock:
        if team_id in _infra_cache:
            return _infra_cache[team_id]
    return provision_team(team_id)
