"""Postgres-backed store for user profiles and artifact associations.

Data is persisted in the shared Khala Postgres instance via
``shared_postgres.get_conn``. DDL lives in ``user_profile.postgres`` and
is registered from the unified_api FastAPI lifespan.

The store is stateless; the connection pool is owned by shared_postgres.
Every public function is wrapped in ``@timed_query`` so slow reads/writes
surface as structured log lines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

from .models import Association, UserProfile, UserProfileUpdate

logger = logging.getLogger(__name__)

_STORE = "user_profile"

#: Single-tenant default until authentication exists. Callers thread a
#: ``user_id`` parameter defaulting to this so real auth can pass a real id
#: later without a data-model change.
DEFAULT_USER_ID = "default"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _ts(value: object) -> str:
    """Render a DB timestamp value as an ISO string (or empty)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@timed_query(store=_STORE, op="get_profile")
def get_profile(user_id: str = DEFAULT_USER_ID) -> UserProfile:
    """Return the profile for ``user_id``, creating it on first read.

    Preconditions:
        - ``user_id`` is a non-empty string.
    Postconditions:
        - A row for ``user_id`` exists in ``user_profiles`` afterward.
        - The returned profile's ``user_id`` equals the argument.
    """
    assert user_id, "user_id must be non-empty"
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT user_id, display_name, email, bio, profile_json, created_at, updated_at "
            "FROM user_profiles WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            now = _now_iso()
            cur.execute(
                "INSERT INTO user_profiles (user_id, created_at, updated_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                (user_id, now, now),
            )
            cur.execute(
                "SELECT user_id, display_name, email, bio, profile_json, created_at, updated_at "
                "FROM user_profiles WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    return UserProfile(
        user_id=row["user_id"],
        display_name=row["display_name"] or "",
        email=row["email"] or "",
        bio=row["bio"] or "",
        preferences=row["profile_json"] or {},
        created_at=_ts(row["created_at"]),
        updated_at=_ts(row["updated_at"]),
    )


@timed_query(store=_STORE, op="upsert_profile")
def upsert_profile(update: UserProfileUpdate, user_id: str = DEFAULT_USER_ID) -> UserProfile:
    """Apply a partial update to ``user_id``'s profile and return it.

    Preconditions:
        - ``user_id`` is a non-empty string.
    Postconditions:
        - Only fields set (non-``None``) on ``update`` are written.
        - ``updated_at`` is advanced.
    """
    assert user_id, "user_id must be non-empty"
    # Ensure the row exists so the UPDATE below always matches.
    get_profile(user_id)

    sets: list[str] = ["updated_at = %s"]
    params: list[object] = [_now_iso()]
    if update.display_name is not None:
        sets.append("display_name = %s")
        params.append(update.display_name)
    if update.email is not None:
        sets.append("email = %s")
        params.append(update.email)
    if update.bio is not None:
        sets.append("bio = %s")
        params.append(update.bio)
    if update.preferences is not None:
        sets.append("profile_json = %s")
        params.append(Json(update.preferences))
    params.append(user_id)

    # UPDATE ... RETURNING builds the result in one round-trip instead of a
    # second SELECT.
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"UPDATE user_profiles SET {', '.join(sets)} WHERE user_id = %s "
            "RETURNING user_id, display_name, email, bio, profile_json, created_at, updated_at",
            params,
        )
        row = cur.fetchone()
    return UserProfile(
        user_id=row["user_id"],
        display_name=row["display_name"] or "",
        email=row["email"] or "",
        bio=row["bio"] or "",
        preferences=row["profile_json"] or {},
        created_at=_ts(row["created_at"]),
        updated_at=_ts(row["updated_at"]),
    )


@timed_query(store=_STORE, op="record_association")
def record_association(
    artifact_type: str,
    team: str,
    artifact_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    label: str = "",
    role: str = "owner",
) -> Association:
    """Idempotently link an artifact to a profile.

    Preconditions:
        - ``artifact_type``, ``team`` and ``artifact_id`` are non-empty.
    Postconditions:
        - Exactly one row exists for ``(user_id, artifact_type, artifact_id)``.
        - Re-recording the same triple refreshes ``label``/``role`` only.
        - Always returns the persisted ``Association`` when preconditions hold.

    A precondition violation is a caller bug and raises ``AssertionError``.
    Callers that cannot tolerate a persistence failure use
    ``record_association_safe``, which never raises.
    """
    assert artifact_type and team and artifact_id, "artifact_type, team, artifact_id required"
    assoc_id = f"assoc_{uuid4().hex[:12]}"
    now = _now_iso()
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO user_profile_associations "
            "(id, user_id, artifact_type, team, artifact_id, label, role, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, artifact_type, artifact_id) DO UPDATE "
            "SET label = EXCLUDED.label, role = EXCLUDED.role "
            "RETURNING id, user_id, artifact_type, team, artifact_id, label, role, created_at",
            (assoc_id, user_id, artifact_type, team, artifact_id, label, role, now),
        )
        row = cur.fetchone()
    return Association(
        id=row["id"],
        user_id=row["user_id"],
        artifact_type=row["artifact_type"],
        team=row["team"],
        artifact_id=row["artifact_id"],
        label=row["label"] or "",
        role=row["role"] or "owner",
        created_at=_ts(row["created_at"]),
    )


def record_association_safe(
    artifact_type: str,
    team: str,
    artifact_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    label: str = "",
    role: str = "owner",
) -> None:
    """Best-effort wrapper around :func:`record_association`.

    A profile-link failure (Postgres disabled, transient error) must never
    break artifact creation, so this swallows and logs every exception.
    """
    try:
        record_association(
            artifact_type,
            team,
            artifact_id,
            user_id=user_id,
            label=label,
            role=role,
        )
    except Exception:  # noqa: BLE001 - best-effort, never propagate
        logger.warning(
            "user_profile: failed to record association type=%s team=%s id=%s",
            artifact_type,
            team,
            artifact_id,
            exc_info=True,
        )


@timed_query(store=_STORE, op="list_associations")
def list_associations(
    user_id: str = DEFAULT_USER_ID,
    artifact_type: Optional[str] = None,
) -> List[Association]:
    """Return a profile's associations, newest first, optionally filtered.

    Preconditions:
        - ``user_id`` is a non-empty string.
    Postconditions:
        - When ``artifact_type`` is given, every result matches it.
    """
    assert user_id, "user_id must be non-empty"
    query = (
        "SELECT id, user_id, artifact_type, team, artifact_id, label, role, created_at "
        "FROM user_profile_associations WHERE user_id = %s"
    )
    params: list[object] = [user_id]
    if artifact_type:
        query += " AND artifact_type = %s"
        params.append(artifact_type)
    query += " ORDER BY created_at DESC"
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [
        Association(
            id=r["id"],
            user_id=r["user_id"],
            artifact_type=r["artifact_type"],
            team=r["team"],
            artifact_id=r["artifact_id"],
            label=r["label"] or "",
            role=r["role"] or "owner",
            created_at=_ts(r["created_at"]),
        )
        for r in rows
    ]


@timed_query(store=_STORE, op="remove_association")
def remove_association(association_id: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Delete one association by id, scoped to ``user_id``.

    Postconditions:
        - Returns ``True`` iff a row was deleted.
    """
    assert association_id, "association_id must be non-empty"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_profile_associations WHERE id = %s AND user_id = %s",
            (association_id, user_id),
        )
        deleted = cur.rowcount
    return deleted > 0
