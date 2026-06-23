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

#: Column list for ``user_profiles``, in the order ``_profile_from_row`` reads.
#: Shared by every SELECT/INSERT/RETURNING so a schema change is edited once.
#: Trusted literal only — never interpolate untrusted input here (it is f-string
#: composed into SQL). Changing these constants (or any query below) requires
#: updating ``user_profile/tests/_fake_postgres.py`` in lockstep — it matches the
#: store's exact SQL text.
_PROFILE_COLUMNS = "user_id, display_name, email, bio, profile_json, created_at, updated_at"

#: Column list for ``user_profile_associations``, in the order ``_assoc_from_row``
#: reads. Shared by record_association (INSERT + RETURNING) and list_associations
#: (SELECT) so a schema change is edited once. Trusted literal only.
_ASSOC_COLUMNS = "id, user_id, artifact_type, team, artifact_id, label, role, created_at"

#: Single-row profile lookup, used twice in ``get_profile`` (initial read + the
#: lost-insert-race re-read). Kept as one literal so both stay identical.
_SELECT_PROFILE_BY_ID = f"SELECT {_PROFILE_COLUMNS} FROM user_profiles WHERE user_id = %s"


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Postconditions:
        - Result is a timezone-aware ISO-8601 string (never empty).
    """
    return datetime.now(tz=timezone.utc).isoformat()


def _ts(value: object) -> str:
    """Render a DB timestamp value as an ISO string.

    Preconditions:
        - ``value`` is a ``datetime`` (from Postgres) or an ISO string (from the
          synthesized cold-start row). Timestamp columns are ``NOT NULL DEFAULT``
          in the schema, so ``value`` is never ``None``.
    Postconditions:
        - A ``datetime`` → its ISO-8601 form; any other value → ``str(value)``.
          Never raises.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _profile_from_row(row: dict) -> UserProfile:
    """Build a ``UserProfile`` from a ``user_profiles`` row dict.

    Preconditions:
        - ``row`` contains every key in :data:`_PROFILE_COLUMNS`. Every text/JSON
          column is ``NOT NULL DEFAULT`` in the schema, so values are never NULL.
    Postconditions:
        - Returns a ``UserProfile`` whose ``user_id`` equals ``row['user_id']``,
          with timestamps ISO-rendered.
    """
    return UserProfile(
        user_id=row["user_id"],
        display_name=row["display_name"],
        email=row["email"],
        bio=row["bio"],
        preferences=row["profile_json"],
        created_at=_ts(row["created_at"]),
        updated_at=_ts(row["updated_at"]),
    )


def _assoc_from_row(row: dict) -> Association:
    """Build an ``Association`` from a ``user_profile_associations`` row dict.

    Preconditions:
        - ``row`` contains the keys ``id, user_id, artifact_type, team,
          artifact_id, label, role, created_at``. ``label`` and ``role`` are
          ``NOT NULL DEFAULT`` in the schema, so they are never NULL.
    Postconditions:
        - Returns an ``Association`` with ``created_at`` ISO-rendered.
    """
    return Association(
        id=row["id"],
        user_id=row["user_id"],
        artifact_type=row["artifact_type"],
        team=row["team"],
        artifact_id=row["artifact_id"],
        label=row["label"],
        role=row["role"],
        created_at=_ts(row["created_at"]),
    )


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
        cur.execute(_SELECT_PROFILE_BY_ID, (user_id,))
        row = cur.fetchone()
        if row is None:
            now = _now_iso()
            cur.execute(
                "INSERT INTO user_profiles (user_id, created_at, updated_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                (user_id, now, now),
            )
            if cur.rowcount == 1:
                # We created the row, so its contents are known — no second SELECT.
                row = {
                    "user_id": user_id,
                    "display_name": "",
                    "email": "",
                    "bio": "",
                    "profile_json": {},
                    "created_at": now,
                    "updated_at": now,
                }
            else:
                # Lost an insert race; read the row the winner created.
                cur.execute(_SELECT_PROFILE_BY_ID, (user_id,))
                row = cur.fetchone()
    # Postcondition: the row exists (INSERT ... ON CONFLICT guarantees it; there
    # is no DELETE path for profiles). A None here is a broken invariant, not a
    # caller error — surface it rather than dereference None.
    assert row is not None, f"user_profiles row missing for {user_id!r} after ensure"
    return _profile_from_row(row)


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

    # Single atomic INSERT ... ON CONFLICT DO UPDATE ... RETURNING:
    #  - first write for a user inserts the row (unset fields take column defaults);
    #  - subsequent writes update only the fields the caller actually set, via the
    #    EXCLUDED.* references in the SET clause (always advancing updated_at).
    # One round-trip, no separate ensure-row read, no TOCTOU window.
    now = _now_iso()
    set_clauses = ["updated_at = EXCLUDED.updated_at"]
    if update.display_name is not None:
        set_clauses.append("display_name = EXCLUDED.display_name")
    if update.email is not None:
        set_clauses.append("email = EXCLUDED.email")
    if update.bio is not None:
        set_clauses.append("bio = EXCLUDED.bio")
    if update.preferences is not None:
        set_clauses.append("profile_json = EXCLUDED.profile_json")

    # `x or default` here only supplies INSERT defaults for a brand-new row; a
    # field is written on conflict *only* if it's non-None (see set_clauses above),
    # so an explicit "" / {} from the caller is still persisted, never coerced away.
    params = (
        user_id,
        update.display_name or "",
        update.email or "",
        update.bio or "",
        Json(update.preferences or {}),
        now,
        now,
    )
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"INSERT INTO user_profiles ({_PROFILE_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (user_id) DO UPDATE SET {', '.join(set_clauses)} "
            f"RETURNING {_PROFILE_COLUMNS}",
            params,
        )
        row = cur.fetchone()
    # Postcondition: INSERT ... ON CONFLICT DO UPDATE always yields the row.
    assert row is not None, f"user_profiles upsert returned no row for {user_id!r}"
    return _profile_from_row(row)


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
    assert user_id, "user_id must be non-empty"
    assert artifact_type and team and artifact_id, "artifact_type, team, artifact_id required"
    # Full 128-bit uuid hex (not a truncated slice): the row's PRIMARY KEY is `id`,
    # but the upsert's ON CONFLICT arbiter is the (user_id, artifact_type, artifact_id)
    # triple. A truncated id raises a non-negligible chance of a *cross-triple* PK
    # collision that the triple arbiter would not absorb (the INSERT would error
    # instead of upserting). Full entropy makes that practically impossible.
    assoc_id = f"assoc_{uuid4().hex}"
    now = _now_iso()
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"INSERT INTO user_profile_associations ({_ASSOC_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, artifact_type, artifact_id) DO UPDATE "
            "SET label = EXCLUDED.label, role = EXCLUDED.role "
            f"RETURNING {_ASSOC_COLUMNS}",
            (assoc_id, user_id, artifact_type, team, artifact_id, label, role, now),
        )
        row = cur.fetchone()
    # Postcondition: INSERT ... ON CONFLICT DO UPDATE always yields the row.
    assert row is not None, "record_association returned no row"
    return _assoc_from_row(row)


def record_association_safe(
    artifact_type: str,
    team: str,
    artifact_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    label: str = "",
    role: str = "owner",
) -> None:
    """Best-effort linking for artifact-create paths.

    Postconditions:
        - Never raises: an *operational* failure (Postgres disabled, transient
          error) is logged and swallowed so it cannot break artifact creation.
        - Invalid inputs (empty ``artifact_type``/``team``/``artifact_id``) are a
          caller bug; they are logged and skipped here rather than passed to
          :func:`record_association` (whose precondition would assert) — so this
          wrapper never hides a contract failure inside a broad ``except``.
    """
    if not (user_id and artifact_type and team and artifact_id):
        logger.warning(
            "user_profile: skipping association with empty fields user=%r type=%r team=%r id=%r",
            user_id,
            artifact_type,
            team,
            artifact_id,
        )
        return
    try:
        record_association(
            artifact_type,
            team,
            artifact_id,
            user_id=user_id,
            label=label,
            role=role,
        )
    except Exception:  # noqa: BLE001 - operational best-effort, never propagate
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
    query = f"SELECT {_ASSOC_COLUMNS} FROM user_profile_associations WHERE user_id = %s"
    params: list[object] = [user_id]
    if artifact_type:
        query += " AND artifact_type = %s"
        params.append(artifact_type)
    query += " ORDER BY created_at DESC"
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [_assoc_from_row(r) for r in rows]


@timed_query(store=_STORE, op="remove_association")
def remove_association(association_id: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Delete one association by id, scoped to ``user_id``.

    Postconditions:
        - Returns ``True`` iff a row was deleted.
    """
    assert association_id, "association_id must be non-empty"
    assert user_id, "user_id must be non-empty"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_profile_associations WHERE id = %s AND user_id = %s",
            (association_id, user_id),
        )
        deleted = cur.rowcount
    return deleted > 0
