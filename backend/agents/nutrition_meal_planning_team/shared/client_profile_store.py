"""Postgres-backed storage for client profiles (Nutrition & Meal Planning team).

Profiles are stored whole in the ``nutrition_profiles.profile`` JSONB
column keyed by ``client_id``. Saves use upsert semantics so there is at
most one current row per client (matching the pre-migration file layout).

SPEC-002 adds:
- Biometric append-only log (``nutrition_biometric_log``) and history query.
- Clinician-override log (``nutrition_clinical_overrides_log``).
- ``profile_version`` monotonic increment on every save.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from shared_postgres import Json, dict_row, get_conn
from shared_postgres.metrics import timed_query

from ..models import BiometricHistoryEntry, ClientProfile

logger = logging.getLogger(__name__)

_STORE = "nutrition_meal_planning"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@timed_query(store=_STORE, op="get_profile")
def get_profile(client_id: str) -> Optional[ClientProfile]:
    """Load client profile by client_id. Returns None if not found."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT profile FROM nutrition_profiles WHERE client_id = %s",
            (client_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
    try:
        profile = ClientProfile.model_validate(row["profile"])
    except Exception:
        logger.warning("Corrupt profile JSON for %s", client_id, exc_info=True)
        return None
    profile.client_id = client_id
    return profile


@timed_query(store=_STORE, op="save_profile")
def save_profile(client_id: str, profile: ClientProfile) -> None:
    """Save client profile via upsert. Mutates ``profile.updated_at`` and
    bumps ``profile_version`` as a side effect.

    SPEC-006: ``restriction_resolver_kb_version`` is a denormalized
    index key mirroring ``profile.restriction_resolution.kb_version``.
    The JSONB ``profile`` column remains the source of truth; this
    column exists only so the background re-resolver can scan without
    parsing every JSONB row.
    """
    profile.client_id = client_id
    profile.updated_at = _now_iso()
    profile.profile_version = (profile.profile_version or 0) + 1
    ts = datetime.now(tz=timezone.utc)
    rr = profile.restriction_resolution
    kb_version = (rr.kb_version or None) if rr is not None else None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nutrition_profiles "
            "(client_id, profile, updated_at, restriction_resolver_kb_version) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (client_id) DO UPDATE "
            "SET profile = EXCLUDED.profile, "
            "    updated_at = EXCLUDED.updated_at, "
            "    restriction_resolver_kb_version = EXCLUDED.restriction_resolver_kb_version",
            (client_id, Json(profile.model_dump()), ts, kb_version),
        )


def create_profile(client_id: str) -> ClientProfile:
    """Create a new empty profile for client_id and save it. Returns the new profile."""
    profile = ClientProfile(client_id=client_id)
    # Start at 0 so the bump in save_profile lands at 1.
    profile.profile_version = 0
    save_profile(client_id, profile)
    return profile


# --- SPEC-002: biometric append-only log ---------------------------------


@timed_query(store=_STORE, op="log_biometric")
def log_biometric(
    client_id: str,
    field: str,
    value_numeric: Optional[float] = None,
    value_text: Optional[str] = None,
    unit: Optional[str] = None,
    source: str = "manual",
    recorded_by: Optional[str] = None,
) -> None:
    """Append one biometric change to nutrition_biometric_log.

    ``field`` is the name of the ClientProfile field that changed
    (e.g. ``"weight_kg"``, ``"activity_level"``, ``"body_fat_pct"``).
    Either ``value_numeric`` or ``value_text`` should be set depending
    on whether the field is numeric.

    No-op at this layer if both values are None — the caller decides
    whether that's an error.
    """
    if value_numeric is None and value_text is None:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nutrition_biometric_log "
            "(client_id, field, value_numeric, value_text, unit, source, recorded_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (client_id, field, value_numeric, value_text, unit, source, recorded_by),
        )


@timed_query(store=_STORE, op="get_biometric_history")
def get_biometric_history(
    client_id: str,
    field: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 200,
) -> List[BiometricHistoryEntry]:
    """Return biometric log rows, newest first.

    When ``field`` is provided, scopes to that field only (common for
    weight-trend charts). ``since`` is an absolute UTC timestamp; if
    None we return the latest ``limit`` rows regardless of age.
    """
    where = ["client_id = %s"]
    params: list = [client_id]
    if field is not None:
        where.append("field = %s")
        params.append(field)
    if since is not None:
        where.append("recorded_at >= %s")
        params.append(since)
    sql = (
        "SELECT field, value_numeric, value_text, unit, source, "
        "       recorded_at, recorded_by "
        "FROM nutrition_biometric_log "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY recorded_at DESC LIMIT %s"
    )
    params.append(limit)
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        BiometricHistoryEntry(
            field=row["field"],
            value_numeric=row["value_numeric"],
            value_text=row["value_text"],
            unit=row["unit"],
            source=row["source"],
            recorded_at=row["recorded_at"].isoformat() if row["recorded_at"] else "",
            recorded_by=row["recorded_by"],
        )
        for row in rows
    ]


# --- SPEC-002: clinician override audit ----------------------------------


@timed_query(store=_STORE, op="log_clinical_override")
def log_clinical_override(
    client_id: str,
    key: str,
    value_numeric: Optional[float],
    author: str = "admin",
    reason: Optional[str] = None,
) -> None:
    """Append one clinician override change to the audit table.

    Called once per key that changed when the admin endpoint writes a
    new override dict to the profile. ``value_numeric`` can be None to
    record a removal.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO nutrition_clinical_overrides_log "
            "(client_id, key, value_numeric, reason, author) "
            "VALUES (%s, %s, %s, %s, %s)",
            (client_id, key, value_numeric, reason, author),
        )


class ClientProfileStore:
    """Postgres-backed store for client profiles.

    The constructor takes no arguments — the Postgres DSN is read from
    ``POSTGRES_*`` env vars by ``shared_postgres.get_conn``.
    """

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    def get_profile(self, client_id: str) -> Optional[ClientProfile]:
        return get_profile(client_id)

    def save_profile(self, client_id: str, profile: ClientProfile) -> None:
        save_profile(client_id, profile)

    def create_profile(self, client_id: str) -> ClientProfile:
        return create_profile(client_id)

    def log_biometric(
        self,
        client_id: str,
        field: str,
        value_numeric: Optional[float] = None,
        value_text: Optional[str] = None,
        unit: Optional[str] = None,
        source: str = "manual",
        recorded_by: Optional[str] = None,
    ) -> None:
        log_biometric(
            client_id,
            field,
            value_numeric=value_numeric,
            value_text=value_text,
            unit=unit,
            source=source,
            recorded_by=recorded_by,
        )

    def get_biometric_history(
        self,
        client_id: str,
        field: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[BiometricHistoryEntry]:
        return get_biometric_history(client_id, field=field, since=since, limit=limit)

    def log_clinical_override(
        self,
        client_id: str,
        key: str,
        value_numeric: Optional[float],
        author: str = "admin",
        reason: Optional[str] = None,
    ) -> None:
        log_clinical_override(client_id, key, value_numeric, author=author, reason=reason)


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[ClientProfileStore] = None


def get_profile_store() -> ClientProfileStore:
    """Return the process-wide store, instantiating on first call."""
    global _default_store
    if _default_store is None:
        _default_store = ClientProfileStore()
    return _default_store
