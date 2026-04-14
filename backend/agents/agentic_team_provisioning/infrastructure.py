"""Per-team infrastructure scaffolding: directories, form store, and job client.

When a team is created via the provisioning API, ``provision_team`` creates:
- ``$AGENT_CACHE/provisioned_teams/{team_id}/assets/``  — file artifacts
- ``$AGENT_CACHE/provisioned_teams/{team_id}/runs/``    — job working directories

Form records are stored in the shared Khala Postgres ``agentic_form_data``
table partitioned by ``team_id``; all operations are idempotent.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from job_service_client import JobServiceClient
from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_AGENT_CACHE = os.getenv("AGENT_CACHE", os.path.join(os.path.expanduser("~"), ".agent_cache"))

_STORE = "agentic_form_data"


# ---------------------------------------------------------------------------
# TeamFormStore — Postgres-backed form records scoped by team_id
# ---------------------------------------------------------------------------


class TeamFormStore:
    """Postgres-backed store for structured form records, scoped to one team."""

    def __init__(self, team_id: str) -> None:
        if not team_id:
            raise ValueError("team_id is required")
        self._team_id = team_id

    @timed_query(store=_STORE, op="create_record")
    def create_record(self, form_key: str, data: Dict[str, Any]) -> Dict[str, Any]:
        record_id = str(uuid4())
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agentic_form_data "
                "(record_id, team_id, form_key, data_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (record_id, self._team_id, form_key, Json(data), now, now),
            )
        return {
            "record_id": record_id,
            "form_key": form_key,
            "data": data,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    @timed_query(store=_STORE, op="get_records")
    def get_records(self, form_key: str) -> List[Dict[str, Any]]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT record_id, form_key, data_json, created_at, updated_at "
                "FROM agentic_form_data "
                "WHERE team_id = %s AND form_key = %s ORDER BY created_at",
                (self._team_id, form_key),
            )
            rows = cur.fetchall()
        return [_row_to_record(r) for r in rows]

    @timed_query(store=_STORE, op="get_record")
    def get_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT record_id, form_key, data_json, created_at, updated_at "
                "FROM agentic_form_data "
                "WHERE team_id = %s AND record_id = %s",
                (self._team_id, record_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    @timed_query(store=_STORE, op="update_record")
    def update_record(self, record_id: str, data: Dict[str, Any]) -> bool:
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_form_data SET data_json = %s, updated_at = %s "
                "WHERE team_id = %s AND record_id = %s",
                (Json(data), now, self._team_id, record_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="delete_record")
    def delete_record(self, record_id: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agentic_form_data WHERE team_id = %s AND record_id = %s",
                (self._team_id, record_id),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="list_form_keys")
    def list_form_keys(self) -> List[str]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT form_key FROM agentic_form_data "
                "WHERE team_id = %s ORDER BY form_key",
                (self._team_id,),
            )
            return [r[0] for r in cur.fetchall()]


def _row_to_record(row: Dict[str, Any]) -> Dict[str, Any]:
    def _ts(v: Any) -> str:
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v or "")

    return {
        "record_id": row["record_id"],
        "form_key": row["form_key"],
        "data": row["data_json"] or {},
        "created_at": _ts(row["created_at"]),
        "updated_at": _ts(row["updated_at"]),
    }


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
    job_client: JobServiceClient
    form_store: TeamFormStore = field(repr=False)


# ---------------------------------------------------------------------------
# Provisioning functions
# ---------------------------------------------------------------------------

_infra_cache: Dict[str, TeamInfrastructure] = {}
_infra_lock = threading.Lock()


def provision_team(team_id: str) -> TeamInfrastructure:
    """Create per-team directories and handles. Idempotent."""
    base = Path(_AGENT_CACHE) / "provisioned_teams" / team_id
    assets_dir = base / "assets"
    runs_dir = base / "runs"

    assets_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    form_store = TeamFormStore(team_id=team_id)
    job_client = JobServiceClient(team=f"provisioned_{team_id}")

    infra = TeamInfrastructure(
        team_id=team_id,
        base_dir=base,
        assets_dir=assets_dir,
        runs_dir=runs_dir,
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
