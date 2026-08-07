"""Per-team infrastructure scaffolding: directories, form store, and job client.

When a team is created via the provisioning API, ``provision_team`` creates
under the process-local cache root ``_AGENT_CACHE``:

- ``.../provisioned_teams/{team_id}/assets/``  — file artifacts
- ``.../provisioned_teams/{team_id}/runs/``    — job working directories

``_AGENT_CACHE`` is initialized once at import from the ``AGENT_CACHE``
environment variable (default ``~/.agent_cache``). Later env changes are
ignored; tests must use ``_set_agent_cache_for_testing``.

Form records are stored in the shared Khala Postgres ``agentic_form_data``
table, scoped by a ``team_id`` column (``WHERE team_id = %s`` filtering, not
Postgres table partitioning). Directory creation and update/delete form
operations are idempotent; ``create_record`` always inserts a new row.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from job_service_client import JobServiceClient
from shared.postgres import get_conn
from shared.postgres.metrics import timed_query

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
    def update_record(self, form_key: str, record_id: str, data: Dict[str, Any]) -> bool:
        """Update a record, scoped to both this team and the given form.

        Postconditions: the record is updated and ``True`` is returned only when
        ``record_id`` resolves to a row for this team AND ``form_key`` — a record_id
        that belongs to a different form under the same team is a no-op.
        """
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agentic_form_data SET data_json = %s, updated_at = %s "
                "WHERE team_id = %s AND record_id = %s AND form_key = %s",
                (Json(data), now, self._team_id, record_id, form_key),
            )
            return cur.rowcount > 0

    @timed_query(store=_STORE, op="delete_record")
    def delete_record(self, form_key: str, record_id: str) -> bool:
        """Delete a record, scoped to both this team and the given form.

        Postconditions: the record is deleted and ``True`` is returned only when
        ``record_id`` resolves to a row for this team AND ``form_key`` — a record_id
        that belongs to a different form under the same team is a no-op.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agentic_form_data WHERE team_id = %s AND record_id = %s "
                "AND form_key = %s",
                (self._team_id, record_id, form_key),
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

# Process-local handle cache. Intentionally not shared across uvicorn workers:
# TeamInfrastructure only holds path handles, a JobServiceClient, and a
# TeamFormStore. Durable state lives in Postgres (forms), the filesystem
# (dirs), and the job service (HTTP). Sibling workers rebuilding equivalent
# handles is correct and requires no distributed cache or cross-process
# invalidation.
_infra_cache: Dict[str, TeamInfrastructure] = {}
_infra_lock = threading.Lock()

# team_id is interpolated into ``.../provisioned_teams/{team_id}/``. Restrict to
# a single path component (alphanumerics, hyphen, underscore) so ``..``,
# separators, and absolute segments cannot escape the provisioned_teams subtree.
_TEAM_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _require_team_id(team_id: str) -> None:
    """Raise ``ValueError`` when ``team_id`` is empty or unsafe for a path component.

    Preconditions:
        - ``team_id`` is the caller-supplied team identifier (may be empty or
          contain arbitrary characters).
    Postconditions:
        - Returns normally only when ``team_id`` is a non-empty string matching
          ``[A-Za-z0-9_-]+`` (safe as a single filesystem path component).
        - Raises ``ValueError`` otherwise (always enforced; not an ``assert``,
          so not stripped by ``python -O``).
    """
    if not team_id:
        raise ValueError("team_id must be a non-empty string")
    if _TEAM_ID_RE.fullmatch(team_id) is None:
        raise ValueError("team_id contains unsafe characters")


def _build_team_infrastructure(team_id: str) -> TeamInfrastructure:
    """Materialize directories and handles for ``team_id`` without touching the cache.

    Preconditions:
        - ``team_id`` is a non-empty string matching ``[A-Za-z0-9_-]+``
          (caller-enforced via ``_require_team_id``).
    Postconditions:
        - ``assets_dir`` and ``runs_dir`` exist under the team base directory,
          which resolves under ``Path(_AGENT_CACHE) / 'provisioned_teams'``.
        - Returns a new ``TeamInfrastructure``; does not read or write ``_infra_cache``.
    """
    provisioned_root = (Path(_AGENT_CACHE) / "provisioned_teams").resolve()
    base = (provisioned_root / team_id).resolve()
    # Defense in depth: charset validation should already prevent escapes, but
    # refuse to mkdir if resolution still leaves the provisioned_teams subtree.
    if not base.is_relative_to(provisioned_root):
        raise ValueError("team_id escapes provisioned_teams directory")
    assets_dir = base / "assets"
    runs_dir = base / "runs"

    assets_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    return TeamInfrastructure(
        team_id=team_id,
        base_dir=base,
        assets_dir=assets_dir,
        runs_dir=runs_dir,
        job_client=JobServiceClient(team=f"provisioned_{team_id}"),
        form_store=TeamFormStore(team_id=team_id),
    )


def provision_team(team_id: str) -> TeamInfrastructure:
    """Create per-team directories and handles. Directory creation is idempotent.

    Preconditions:
        - ``team_id`` is a non-empty string matching ``[A-Za-z0-9_-]+``.
    Postconditions:
        - ``assets_dir`` and ``runs_dir`` exist under the team base directory.
        - The returned ``TeamInfrastructure`` is stored in the process-local
          ``_infra_cache`` under ``team_id`` (replacing any prior entry).
        - Form records and job state remain in Postgres / the job service;
          this function only materializes local handles and directories.
    """
    _require_team_id(team_id)
    infra = _build_team_infrastructure(team_id)

    with _infra_lock:
        _infra_cache[team_id] = infra

    logger.info("Provisioned infrastructure for team %s at %s", team_id, infra.base_dir)
    return infra


def get_team_infrastructure(team_id: str) -> TeamInfrastructure:
    """Return process-local infrastructure for a team, provisioning lazily if needed.

    Preconditions:
        - ``team_id`` is a non-empty string matching ``[A-Za-z0-9_-]+``.
    Postconditions:
        - Returns a ``TeamInfrastructure`` for ``team_id``.
        - Within a single process, repeated calls for the same ``team_id``
          return the same instance after the first successful lazy provision,
          provided no intervening ``provision_team(team_id)`` (which replaces
          the cache entry) and the process-local cache has not been cleared
          for tests.
        - Concurrent cache misses double-check under ``_infra_lock`` so only
          one instance is published; losers discard their built handles and
          return the winner.
        - Directory creation is idempotent; multi-worker divergence of the
          in-memory map is safe because durable state is external.
    """
    _require_team_id(team_id)
    with _infra_lock:
        cached = _infra_cache.get(team_id)
        if cached is not None:
            return cached

    infra = _build_team_infrastructure(team_id)
    with _infra_lock:
        cached = _infra_cache.get(team_id)
        if cached is not None:
            return cached
        _infra_cache[team_id] = infra

    logger.info("Provisioned infrastructure for team %s at %s", team_id, infra.base_dir)
    return infra


def _set_agent_cache_for_testing(path: str) -> None:
    """Set the process-local agent-cache root. Test-only isolation seam.

    Preconditions:
        - ``path`` is a non-empty filesystem path string.
    Postconditions:
        - Subsequent ``_build_team_infrastructure`` calls root under ``path``.
    """
    if not path:
        raise ValueError("path must be a non-empty string")
    global _AGENT_CACHE
    _AGENT_CACHE = path


def _clear_infra_cache_for_testing() -> None:
    """Drop all cached team infrastructure handles. Test-only isolation seam."""
    with _infra_lock:
        _infra_cache.clear()
